"""Pruebas sin acceso a los servicios ni a los datos reales del equipo."""
from concurrent.futures import Future
import contextlib
import datetime
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src import monitor


def sample_data():
    return {
        'batteries': [],
        'networks': ([('eth0', '192.168.1.10', 24, 'UP')], None),
        'wifi': (None, 'Sin conexión Wi-Fi'),
        'containers': ([], None),
        'preferred': 'eth0',
        'os': 'Linux de prueba',
        'cpu': (25, {'cpu0': 25, 'cpu1': 25}),
        'physical': 1,
        'logical': 2,
        'memory': (1024 ** 3, 2 * 1024 ** 3),
        'disk': None,
        'uptime': '1 h 0 min',
        'kernel': 'test-kernel',
        'architecture': 'test-arch',
        'collected_at': datetime.datetime(2026, 10, 29, 14, 32, 5,
                                          tzinfo=datetime.timezone.utc),
    }


class MetricsTests(unittest.TestCase):
    def test_cpu_percent(self):
        self.assertEqual(monitor.cpu_percent((100, 50), (200, 75)), 75)
        self.assertIsNone(monitor.cpu_percent((100, 50), (100, 50)))
        self.assertEqual(monitor.cpu_percent((100, 50), (200, 200)), 0)

    def test_cpu_counters_do_not_count_guest_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stat'
            path.write_text('cpu 10 0 20 50 5 2 3 10 4 1\n'
                            'cpu0 10 0 20 50 5 2 3 10 4 1\nintr 123\n')
            self.assertEqual(monitor.cpu_counters(path)['cpu'], (100, 55))

    def test_unreadable_cpu_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(monitor.cpu_counters(Path(directory) / 'missing'))

    def test_battery_capacity_is_clamped_and_missing_capacity_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('BAT0', 'BAT1'):
                battery = root / name
                battery.mkdir()
                (battery / 'type').write_text('Battery\n')
                (battery / 'status').write_text('Charging\n')
            (root / 'BAT0' / 'capacity').write_text('150\n')
            self.assertEqual(monitor.battery_info(root), [
                ('BAT0', 100, 'Cargando'), ('BAT1', None, 'Cargando')])

    def test_physical_cores_distinguish_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cpuinfo'
            path.write_text('physical id : 0\ncore id : 0\n\n'
                            'physical id : 0\ncore id : 0\n\n'
                            'physical id : 1\ncore id : 0\n')
            self.assertEqual(monitor.physical_cores(path), 2)

    def test_os_pretty_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'os-release'
            path.write_text('PRETTY_NAME="Linux de prueba 1.0"\n')
            self.assertEqual(monitor.os_info(path), 'Linux de prueba 1.0')

    def test_size_and_color_thresholds(self):
        self.assertEqual(monitor.size_gib(1024 ** 3), '1.0 GiB')
        self.assertEqual([monitor.percent_color(n) for n in (0, 74, 75, 89, 90, 100)],
                         [1, 1, 3, 3, 4, 4])


class ExternalCommandsTests(unittest.TestCase):
    @patch('src.monitor.subprocess.run')
    def test_network_ignores_loopback(self, run):
        run.return_value.stdout = ('[{"ifname":"lo","addr_info":[]},'
                                   '{"ifname":"eth0","operstate":"UP",'
                                   '"addr_info":[{"family":"inet",'
                                   '"local":"192.168.1.10","prefixlen":24}]}]')
        self.assertEqual(monitor.network_info(),
                         ([('eth0', '192.168.1.10', 24, 'UP')], None))
        self.assertEqual(run.call_args.kwargs['timeout'], 3)

    @patch('src.monitor.subprocess.run')
    def test_network_invalid_json(self, run):
        run.return_value.stdout = 'not json'
        rows, error = monitor.network_info()
        self.assertEqual(rows, [])
        self.assertIn('No se pudo consultar ip', error)

    @patch('src.monitor.subprocess.run')
    def test_missing_optional_commands(self, run):
        run.side_effect = FileNotFoundError()
        self.assertEqual(monitor.docker_info(), ([], 'Docker no está instalado'))
        self.assertEqual(monitor.wifi_info(), (None, 'SSID no disponible (falta nmcli)'))
        self.assertIsNone(monitor.default_interface())

    @patch('src.monitor.subprocess.run')
    def test_docker_timeout_and_permissions(self, run):
        run.side_effect = subprocess.TimeoutExpired('docker', 3)
        self.assertEqual(monitor.docker_info(), ([], 'Docker no respondió a tiempo'))
        run.side_effect = subprocess.CalledProcessError(
            1, 'docker', stderr='permission denied')
        self.assertEqual(monitor.docker_info(), ([], 'Sin permiso para consultar Docker'))

    @patch('src.monitor.subprocess.run')
    def test_wifi_does_not_rescan(self, run):
        run.return_value.stdout = '*:Mi Wi-Fi\n'
        self.assertEqual(monitor.wifi_info(), ('Mi Wi-Fi', None))
        self.assertEqual(run.call_args.args[0][-2:], ['--rescan', 'no'])


class SnapshotTests(unittest.TestCase):
    def test_basic_presentation(self):
        lines = monitor.format_snapshot(sample_data())
        text = '\n'.join(line for line, _ in lines)
        self.assertIn('CPU          25%', text)
        self.assertIn('1 físicos · 2 lógicos', text)
        self.assertIn('1.0 GiB / 2.0 GiB', text)
        self.assertIn('eth0  192.168.1.10/24', text)
        self.assertIn('0 contenedores activos', text)
        self.assertNotIn('test-kernel', text)

    def test_details_and_containers(self):
        data = sample_data()
        data['containers'] = ([('web', 'nginx', 'Up 1 hour', '80/tcp')], None)
        text = '\n'.join(line for line, _ in monitor.format_snapshot(data, True))
        self.assertIn('1 contenedor activo', text)
        self.assertIn('test-kernel', text)
        self.assertIn('Imagen: nginx', text)
        self.assertIn('Puertos: 80/tcp', text)
        self.assertIn('USO POR NÚCLEO LÓGICO', text)

    def test_unavailable_metrics(self):
        data = sample_data()
        data.update(cpu=(None, {}), memory=None, physical=None, logical=None,
                    networks=([], 'Error de red'), containers=([], 'Docker ausente'))
        text = '\n'.join(line for line, _ in monitor.format_snapshot(data))
        self.assertIn('CPU          No disponible', text)
        self.assertIn('RAM          No disponible', text)
        self.assertIn('Error de red', text)
        self.assertIn('Docker ausente', text)

    def test_collection_uses_collectors(self):
        data = sample_data()
        names = {'battery_info': 'batteries', 'network_info': 'networks',
                 'wifi_info': 'wifi', 'docker_info': 'containers',
                 'default_interface': 'preferred', 'os_info': 'os',
                 'cpu_usage': 'cpu', 'physical_cores': 'physical',
                 'memory_info': 'memory', 'disk_info': 'disk', 'uptime_info': 'uptime'}
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(monitor, name, return_value=data[key]))
                     for name, key in names.items()]
            result = monitor.collect_data()
        for mock in mocks:
            mock.assert_called_once_with()
        for key in names.values():
            self.assertEqual(result[key], data[key])
        self.assertEqual(result['logical'], 2)
        self.assertIsNotNone(result['collected_at'].tzinfo)

    def test_timestamp_is_shown_in_text_output(self):
        text = '\n'.join(line for line, _ in monitor.format_snapshot(sample_data()))
        self.assertIn('Última actualización: 29/10/2026 14:32:05 +0000', text)

    @patch('src.monitor.collect_data')
    def test_snapshot_connects_collection_and_formatting(self, collect):
        collect.return_value = sample_data()
        self.assertEqual(monitor.snapshot(True),
                         monitor.format_snapshot(sample_data(), True))


class SummaryTests(unittest.TestCase):
    def test_healthy_metrics(self):
        data = sample_data()
        data['disk'] = SimpleNamespace(used=20, total=100)
        self.assertIn(('Sin alertas en las métricas comprobadas', 1),
                      monitor.summary_lines(data))

    def test_percentage_thresholds(self):
        for percent, color in [(74, None), (75, 3), (89, 3), (90, 4), (100, 4)]:
            with self.subTest(percent=percent):
                data = sample_data()
                data.update(cpu=(percent, {}), memory=(percent, 100),
                            disk=SimpleNamespace(used=percent, total=100))
                lines = monitor.summary_lines(data)
                for label in ('CPU', 'RAM', 'Disco /'):
                    matches = [(text, shade) for text, shade in lines
                               if f'{label} al ' in text]
                    if color is None:
                        self.assertEqual(matches, [])
                    else:
                        self.assertEqual(len(matches), 1)
                        self.assertEqual(matches[0][1], color)

    def test_battery_thresholds_and_unknown_capacity(self):
        data = sample_data()
        data['batteries'] = [('BAT0', 10, 'Descargando'), ('BAT1', 20, 'Cargando'),
                             ('BAT2', 21, 'Descargando'), ('BAT3', None, 'Desconocido')]
        lines = monitor.summary_lines(data)
        self.assertIn(('CRÍTICO: BAT0 al 10 % (Descargando)', 4), lines)
        self.assertIn(('AVISO: BAT1 al 20 % (Cargando)', 3), lines)
        self.assertIn(('AVISO: porcentaje de BAT3 no disponible', 3), lines)
        self.assertFalse(any('BAT2' in text for text, _ in lines))

    def test_missing_metrics_and_failed_commands_are_not_healthy(self):
        data = sample_data()
        data.update(cpu=(None, {}), memory=(0, 0), disk=None,
                    networks=([], 'Error de red'), containers=([], 'Docker ausente'))
        lines = monitor.summary_lines(data)
        text = '\n'.join(line for line, _ in lines)
        for label in ('CPU', 'RAM', 'Disco /'):
            self.assertIn(f'{label} no disponible', text)
        self.assertIn('Error de red', text)
        self.assertIn('Docker ausente', text)
        self.assertNotIn('Sin alertas', text)


class ScreenTests(unittest.TestCase):
    def run_frames(self, futures, keys):
        window = MagicMock()
        window.getmaxyx.return_value = (80, 120)
        window.getch.side_effect = keys
        frames = []
        current = []
        window.erase.side_effect = current.clear
        window.addnstr.side_effect = lambda y, x, text, width, attr: current.append(text)
        window.refresh.side_effect = lambda: frames.append(list(current))
        executor = MagicMock()
        executor.submit.side_effect = futures
        with patch.object(monitor.curses, 'curs_set'), \
                patch.object(monitor.curses, 'has_colors', return_value=False), \
                patch.object(monitor.time, 'monotonic', return_value=0):
            monitor.run_screen(window, 2, executor)
        return frames

    def test_initial_pending_indicator(self):
        frames = self.run_frames([Future()], [ord('q')])
        self.assertIn('Consultando primera muestra…', frames[0])

    def test_refresh_keeps_previous_timestamp_while_pending(self):
        complete = Future()
        complete.set_result(sample_data())
        frames = self.run_frames([complete, Future()], [-1, ord('r'), ord('q')])
        self.assertIn('Actualizado: 14:32:05', frames[1])
        self.assertIn('Actualizando…', frames[2])
        self.assertIn('Última actualización: 29/10/2026 14:32:05 +0000', frames[2])

    def test_failure_keeps_previous_data_and_recovers(self):
        first, failed, recovered = Future(), Future(), Future()
        first.set_result(sample_data())
        failed.set_exception(RuntimeError('Consulta fallida'))
        new_data = sample_data()
        new_data['collected_at'] += datetime.timedelta(seconds=10)
        recovered.set_result(new_data)
        frames = self.run_frames([first, failed, recovered],
                                 [-1, ord('r'), -1, ord('r'), -1, ord('q')])
        self.assertIn('ERROR: datos anteriores', frames[3])
        self.assertIn('RuntimeError: Consulta fallida', frames[3])
        self.assertIn('CPU          25%', frames[3])
        self.assertIn('Última actualización: 29/10/2026 14:32:05 +0000', frames[3])
        self.assertIn('Actualizado: 14:32:15', frames[-1])
        self.assertNotIn('ERROR: datos anteriores', frames[-1])

    def test_first_failure_retries_automatically(self):
        failed, recovered = Future(), Future()
        failed.set_exception(RuntimeError('Consulta fallida'))
        recovered.set_result(sample_data())
        window = MagicMock()
        window.getmaxyx.return_value = (80, 120)
        window.getch.side_effect = [-1, -1, -1, ord('q')]
        executor = MagicMock()
        executor.submit.side_effect = [failed, recovered]
        # 1: enviar; 2: fallo y programar; 3: vence la espera; 4: recuperar.
        with patch.object(monitor.curses, 'curs_set'), \
                patch.object(monitor.curses, 'has_colors', return_value=False), \
                patch.object(monitor.time, 'monotonic', side_effect=[0, 0, 0, 2, 2, 2]):
            monitor.run_screen(window, 2, executor)
        rendered = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertIn('ERROR: sin datos', rendered)
        self.assertIn('Actualizado: 14:32:05', rendered)
        self.assertEqual(executor.submit.call_count, 2)

    @patch('src.monitor.curses.curs_set')
    @patch('src.monitor.curses.has_colors', return_value=False)
    def test_keyboard_works_while_collection_is_pending(self, *_):
        window = MagicMock()
        window.getmaxyx.return_value = (24, 80)
        window.getch.return_value = ord('q')
        executor = MagicMock()
        pending = Future()
        executor.submit.return_value = pending
        monitor.run_screen(window, 2, executor)
        executor.submit.assert_called_once_with(monitor.collect_data)
        self.assertFalse(pending.done())
        window.refresh.assert_called_once_with()

    @patch('src.monitor.curses.curs_set')
    @patch('src.monitor.curses.has_colors', return_value=False)
    def test_completed_collection_is_displayed(self, *_):
        window = MagicMock()
        window.getmaxyx.return_value = (40, 100)
        window.getch.side_effect = [-1, ord('q')]
        executor = MagicMock()
        pending = Future()
        pending.set_result(sample_data())
        executor.submit.return_value = pending
        monitor.run_screen(window, 2, executor)
        rendered = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertIn('CPU          25%', rendered)


if __name__ == '__main__':
    unittest.main()
