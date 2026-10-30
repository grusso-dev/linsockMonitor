#!/usr/bin/env python3
"""Panel de estado del equipo para Linux. Sin dependencias de pip."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import curses
import datetime
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import time


def battery_info(root=Path('/sys/class/power_supply')):
    batteries = []
    states = {'Charging': 'Cargando', 'Discharging': 'Descargando',
              'Full': 'Carga completa', 'Not charging': 'Sin cargar',
              'Unknown': 'Desconocido'}
    for directory in sorted(root.glob('*')):
        try:
            if (directory / 'type').read_text().strip() != 'Battery':
                continue
            status = (directory / 'status').read_text().strip()
            try:
                percent = max(0, min(100, int((directory / 'capacity').read_text())))
            except (OSError, ValueError):
                percent = None
            batteries.append((directory.name, percent, states.get(status, status)))
        except OSError:
            continue
    return batteries


def network_info():
    try:
        result = subprocess.run(['ip', '-j', '-4', 'address', 'show'],
                                capture_output=True, text=True, timeout=3, check=True)
        rows = []
        for interface in json.loads(result.stdout):
            if interface['ifname'] == 'lo':
                continue
            for address in interface.get('addr_info', []):
                if address.get('family') == 'inet':
                    rows.append((interface['ifname'], address['local'],
                                 address['prefixlen'], interface.get('operstate', '?')))
        return rows, None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        return [], f'No se pudo consultar ip: {exc}'


def default_interface():
    try:
        result = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'default'],
                                capture_output=True, text=True, timeout=3, check=True)
        for route in json.loads(result.stdout):
            if route.get('dev'):
                return route['dev']
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    return None


def wifi_info():
    """Obtiene el SSID conectado sin forzar un nuevo escaneo de redes."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-e', 'no', '-f', 'IN-USE,SSID',
             'device', 'wifi', 'list', '--rescan', 'no'],
            capture_output=True, text=True, timeout=3, check=True)
        for line in result.stdout.splitlines():
            if line.startswith('*:'):
                ssid = ''.join(char if char.isprintable() else '?' for char in line[2:])
                return ssid or '(red oculta)', None
        return None, 'Sin conexión Wi-Fi'
    except FileNotFoundError:
        return None, 'SSID no disponible (falta nmcli)'
    except (OSError, subprocess.SubprocessError):
        return None, 'SSID no disponible'


def os_info(root=Path('/etc/os-release')):
    """Lee la identificación de la distribución sin ejecutar otros programas."""
    for path in (root, Path('/usr/lib/os-release')):
        try:
            values = {}
            for line in path.read_text().splitlines():
                key, separator, value = line.partition('=')
                if separator and key in ('PRETTY_NAME', 'NAME', 'VERSION_ID'):
                    try:
                        parsed = shlex.split(value)
                        if parsed:
                            values[key] = parsed[0]
                    except ValueError:
                        continue
            if values.get('PRETTY_NAME'):
                return values['PRETTY_NAME']
            if values.get('NAME'):
                version = values.get('VERSION_ID', '')
                return f"{values['NAME']} {version}".strip()
        except OSError:
            continue
    return platform.system() or 'No disponible'


def docker_info():
    """Consulta únicamente los contenedores en ejecución, sin usar sudo."""
    try:
        result = subprocess.run(
            ['docker', 'ps', '--filter', 'status=running', '--format', '{{json .}}'],
            capture_output=True, text=True, timeout=3, check=True)
        containers = []
        for line in result.stdout.splitlines():
            item = json.loads(line)
            containers.append((item['Names'], item['Image'], item['Status'],
                               item.get('Ports', '')))
        return containers, None
    except FileNotFoundError:
        return [], 'Docker no está instalado'
    except subprocess.TimeoutExpired:
        return [], 'Docker no respondió a tiempo'
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or '').lower()
        if 'permission denied' in message:
            return [], 'Sin permiso para consultar Docker'
        return [], 'No se pudo conectar con Docker (¿está iniciado?)'
    except (OSError, ValueError, KeyError, TypeError):
        return [], 'No se pudo leer la respuesta de Docker'


def cpu_counters(root=Path('/proc/stat')):
    try:
        counters = {}
        with root.open() as file:
            for line in file:
                fields = line.split()
                if not fields or not fields[0].startswith('cpu'):
                    break
                name = fields[0]
                if name != 'cpu' and not name[3:].isdigit():
                    continue
                values = list(map(int, fields[1:]))
                counters[name] = (sum(values[:8]), values[3] + values[4])
        return counters if 'cpu' in counters else None
    except (OSError, ValueError, IndexError):
        return None


def physical_cores(root=Path('/proc/cpuinfo')):
    try:
        cores = set()
        for section in root.read_text().split('\n\n'):
            fields = {key.strip(): value.strip() for line in section.splitlines() if ':' in line
                      for key, value in [line.split(':', 1)]}
            package = fields.get('physical id')
            core = fields.get('core id')
            if package is not None and core is not None:
                cores.add((package, core))
        return len(cores) or None
    except OSError:
        return None


def cpu_percent(first, second):
    total = second[0] - first[0]
    idle = second[1] - first[1]
    return max(0, min(100, round(100 * (total - idle) / total))) if total > 0 else None


def cpu_usage():
    first = cpu_counters()
    if first is None:
        return None, {}
    time.sleep(0.1)
    second = cpu_counters()
    if second is None:
        return None, {}
    usage = {name: cpu_percent(first[name], values) for name, values in second.items()
             if name in first}
    cores = {name: value for name, value in usage.items() if name != 'cpu'}
    return usage.get('cpu'), cores


def memory_info():
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, _, value = line.partition(':')
            if name in ('MemTotal', 'MemAvailable'):
                values[name] = int(value.split()[0]) * 1024
        total = values['MemTotal']
        used = total - values['MemAvailable']
        return used, total
    except (OSError, ValueError, KeyError, IndexError):
        return None


def uptime_info():
    try:
        seconds = int(float(Path('/proc/uptime').read_text().split()[0]))
        days, remainder = divmod(seconds, 86400)
        hours, minutes = divmod(remainder, 3600)
        return f'{days} d {hours} h {minutes // 60} min' if days else f'{hours} h {minutes // 60} min'
    except (OSError, ValueError, IndexError):
        return 'No disponible'


def size_gib(value):
    return f'{value / 1024 ** 3:.1f} GiB'


def percent_color(percent):
    return 4 if percent >= 90 else 3 if percent >= 75 else 1


def disk_info():
    try:
        return shutil.disk_usage('/')
    except OSError:
        return None


def collect_data():
    """Recoge los datos en paralelo, sin decidir cómo se mostrarán."""
    collectors = {
        'batteries': battery_info,
        'networks': network_info,
        'wifi': wifi_info,
        'containers': docker_info,
        'preferred': default_interface,
        'os': os_info,
        'cpu': cpu_usage,
        'physical': physical_cores,
        'memory': memory_info,
        'disk': disk_info,
        'uptime': uptime_info,
    }
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {name: executor.submit(collector)
                   for name, collector in collectors.items()}
        data = {name: future.result() for name, future in futures.items()}
    data['logical'] = len(data['cpu'][1]) or os.cpu_count()
    data['kernel'] = platform.release()
    data['architecture'] = platform.machine()
    return data


def format_snapshot(data, details=False):
    """Convierte los datos recogidos en líneas de texto y colores."""
    batteries = data['batteries']
    networks, error = data['networks']
    ssid, wifi_error = data['wifi']
    containers, docker_error = data['containers']
    preferred = data['preferred']
    main_network = next((row for row in networks if row[0] == preferred), None)
    if main_network is None:
        main_network = next((row for row in networks if row[3] == 'UP'
                             and row[0].startswith(('wl', 'en', 'eth', 'ww'))), None)
    lines = [('SISTEMA', 2), (f"SO           {data['os']}", 0)]
    cpu, core_usage = data['cpu']
    lines.append((f'CPU          {cpu}%' if cpu is not None else 'CPU          No disponible',
                  percent_color(cpu) if cpu is not None else 0))
    logical = data['logical']
    physical = data['physical']
    if physical and logical:
        core_text = f'{physical} físicos · {logical} lógicos'
    elif logical:
        core_text = f'{logical} lógicos'
    else:
        core_text = 'No disponible'
    lines.append((f'Núcleos      {core_text}', 0))
    memory = data['memory']
    if memory:
        used, total = memory
        lines.append((f'RAM          {size_gib(used)} / {size_gib(total)}',
                      percent_color(100 * used / total) if total else 0))
    else:
        lines.append(('RAM          No disponible', 0))
    disk = data['disk']
    if disk is not None:
        lines.append((f'Disco /      {size_gib(disk.used)} / {size_gib(disk.total)}',
                      percent_color(100 * disk.used / disk.total) if disk.total else 0))
    else:
        lines.append(('Disco /      No disponible', 0))
    lines.append((f"Encendido    {data['uptime']}", 0))
    if details:
        lines.append((f"Núcleo       {data['kernel']}", 0))
        lines.append((f"Arquitectura {data['architecture']}", 0))
        if core_usage:
            lines.extend([('', 0), ('USO POR NÚCLEO LÓGICO', 2)])
            for name, usage in core_usage.items():
                label = f'CPU {name[3:]:<8}'
                lines.append((f'{label} {usage}%' if usage is not None else f'{label} No disponible',
                              percent_color(usage) if usage is not None else 0))
    lines.extend([('', 0), ('ENERGÍA', 2)])
    if not batteries:
        lines.append(('Sin batería detectada', 0))
    for name, percent, status in batteries:
        lines.append((f'{name} · {status}', 0))
        if percent is None:
            lines.append(('Porcentaje no disponible', 0))
        else:
            filled = round(percent / 5)
            bar = '█' * filled + '░' * (20 - filled)
            lines.append((f'{bar}  {percent}%', 4 if percent <= 10 else 3 if percent <= 20 else 1))
    lines.extend([('', 0), ('RED · IPv4 LOCAL', 2)])
    if error:
        lines.append((error, 3))
    elif main_network:
        name, ip, prefix, state = main_network
        lines.append((f'{name}  {ip}/{prefix}  [{state}]', 1))
    else:
        lines.append(('Sin conexión principal IPv4 detectada', 0))
    lines.append((f'Wi-Fi        {ssid}' if ssid is not None else f'Wi-Fi        {wifi_error}',
                  1 if ssid is not None else 0))
    lines.extend([('', 0), ('DOCKER', 2)])
    if docker_error:
        lines.append((docker_error, 3))
    else:
        count = len(containers)
        lines.append((f'{count} contenedor' + (' activo' if count == 1 else 'es activos'), 1))
    if details:
        if containers:
            lines.extend([('', 0), ('DETALLES · CONTENEDORES ACTIVOS', 2)])
            for name, image, status, ports in containers:
                lines.append((f'{name} · {status}', 1))
                lines.append((f'  Imagen: {image}', 0))
                if ports:
                    lines.append((f'  Puertos: {ports}', 0))
        lines.extend([('', 0), ('DETALLES · TODAS LAS INTERFACES', 2)])
        if not networks and not error:
            lines.append(('Sin direcciones IPv4 asignadas', 0))
        for name, ip, prefix, state in networks:
            lines.append((f'{name}  {ip}/{prefix}  [{state}]', 1 if state == 'UP' else 0))
    return lines


def snapshot(details=False):
    """Obtiene y prepara una muestra para el modo de texto."""
    return format_snapshot(collect_data(), details)


def screen(stdscr, interval):
    # El hilo principal queda libre para dibujar y atender el teclado.
    with ThreadPoolExecutor(max_workers=1) as executor:
        run_screen(stdscr, interval, executor)


def run_screen(stdscr, interval, executor):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    stdscr.timeout(200)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for number, color in [(1, curses.COLOR_GREEN), (2, curses.COLOR_CYAN),
                              (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED)]:
            curses.init_pair(number, color, -1)
    next_refresh = 0
    lines = [('Recogiendo información…', 0)]
    data = None
    pending = None
    details = False
    scroll = 0
    while True:
        if pending is not None and pending.done():
            data = pending.result()
            pending = None
            lines = format_snapshot(data, details)
            next_refresh = time.monotonic() + interval
        if pending is None and time.monotonic() >= next_refresh:
            pending = executor.submit(collect_data)
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height >= 4 and width >= 8:
            stdscr.border()

        def put(y, value, color=0, bold=False):
            if 0 <= y < height - 1 and width > 4:
                attr = curses.color_pair(color) if curses.has_colors() else 0
                if bold:
                    attr |= curses.A_BOLD
                try:
                    stdscr.addnstr(y, 2, value, width - 4, attr)
                except curses.error:
                    pass

        put(1, 'ESTADO DEL EQUIPO', 2, True)
        put(2, f'{socket.gethostname()} · {datetime.datetime.now():%d/%m/%Y %H:%M:%S}')
        put(3, '─' * max(0, width - 4), 2)
        put(height - 3, '─' * max(0, width - 4), 2)
        visible = max(0, height - 8)
        scroll = min(scroll, max(0, len(lines) - visible)) if visible else 0
        if height < 12 or width < 36:
            put(5, 'Ampliá la terminal para ver el panel.', 3)
        else:
            for y, (value, color) in enumerate(lines[scroll:scroll + visible], 5):
                put(y, value, color, color == 2)
        put(height - 2, f'q salir · r actualizar · d detalles · ↑↓ mover · {interval:g}s', 2)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        if key in (ord('d'), ord('D')):
            details = not details
            scroll = 0
            if data is not None:
                lines = format_snapshot(data, details)
        if key in (curses.KEY_DOWN, ord('j')):
            scroll = min(scroll + 1, max(0, len(lines) - visible))
        if key in (curses.KEY_UP, ord('k')):
            scroll = max(0, scroll - 1)
        if key in (ord('r'), ord('R'), curses.KEY_RESIZE):
            next_refresh = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Mostrar una vez, sin interfaz')
    parser.add_argument('--details', action='store_true', help='Incluir detalles adicionales con --once')
    parser.add_argument('--interval', type=float, default=2,
                        help='Espera entre muestras en segundos (0.5 a 3600; predeterminado: 2)')
    args = parser.parse_args()
    if not 0.5 <= args.interval <= 3600:
        parser.error('--interval debe estar entre 0.5 y 3600')
    if args.once:
        for line, _ in snapshot(args.details):
            print(line)
        return
    try:
        curses.wrapper(screen, args.interval)
    except KeyboardInterrupt:
        pass
    except curses.error:
        parser.exit(1, 'No se pudo abrir la interfaz. Usá una terminal o --once.\n')


if __name__ == '__main__':
    main()
