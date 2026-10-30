"""Pruebas sin acceso a los servicios ni a los datos reales del equipo."""
from concurrent.futures import Future
import contextlib
from pathlib import Path
import subprocess
import tempfile
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

    @patch('src.monitor.collect_data')
    def test_snapshot_connects_collection_and_formatting(self, collect):
        collect.return_value = sample_data()
        self.assertEqual(monitor.snapshot(True),
                         monitor.format_snapshot(sample_data(), True))


class ScreenTests(unittest.TestCase):
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
