# linsockMonitor

Monitor de estado para Linux, escrito en Python. Muestra información del equipo
en una pantalla de terminal que se actualiza automáticamente, o como texto en una
única consulta. No requiere paquetes de `pip` ni utiliza `sudo`.

## Qué muestra

- Sistema operativo, CPU, núcleos físicos y lógicos.
- Memoria RAM, espacio ocupado en el disco raíz (`/`) y tiempo encendido.
- Estado y porcentaje de las baterías disponibles.
- IPv4 local de la interfaz principal y nombre de la red Wi-Fi (SSID).
- Cantidad de contenedores Docker en ejecución.
- En detalles: kernel, arquitectura, uso por CPU lógica, contenedores con imagen
  y puertos, y todas las interfaces con IPv4 excepto la de bucle local (`lo`).

## Requisitos

- Linux con `/proc` y `/sys` disponibles.
- Python 3.8 o posterior con el módulo estándar `curses`. Las pruebas se han
  ejecutado con Python 3.11; no se han comprobado todas las versiones anteriores.
- Una terminal para la pantalla interactiva; no es necesaria con `--once`.

Algunas consultas utilizan programas externos:

| Programa | Para qué se utiliza | Si no está disponible |
| --- | --- | --- |
| `ip` (paquete `iproute2` en Debian/Ubuntu) | Direcciones y ruta IPv4 | Se indica que no se pudo consultar la red |
| `nmcli` (NetworkManager) | Nombre de la Wi-Fi conectada | El SSID aparece como no disponible |
| `docker` | Contenedores en ejecución | Se muestra un aviso en la sección Docker |

No necesitas instalar Docker ni NetworkManager para usar las demás secciones.
Docker debe estar iniciado y tu usuario debe tener permiso para consultarlo.
No cambies permisos del socket ni ejecutes el monitor como administrador solo
para ocultar un aviso: el acceso a Docker puede otorgar privilegios elevados.

## Primeros pasos

Abre una terminal en la carpeta del proyecto. Comprueba que tienes Python:

```bash
python3 --version
```

Muestra una consulta sin abrir la pantalla interactiva:

```bash
python3 src/monitor.py --once
```

Abre el panel:

```bash
python3 src/monitor.py
```

No hay un paso de instalación ni hace falta crear un entorno virtual para usarlo.
Los comandos anteriores deben ejecutarse desde la raíz del repositorio.

### Opciones

```bash
# Texto con detalles adicionales
python3 src/monitor.py --once --details

# Esperar cinco segundos entre muestras en el panel
python3 src/monitor.py --interval 5

# Consultar la ayuda
python3 src/monitor.py --help
```

| Opción | Comportamiento |
| --- | --- |
| `--once` | Imprime una muestra y termina |
| `--details` | Añade detalles a `--once`; en el panel se usa la tecla `d` |
| `--interval SEGUNDOS` | Espera entre muestras del panel; predeterminado: 2, rango: 0.5–3600 |

`--interval` no afecta al modo `--once`. El tiempo real entre muestras incluye
también lo que tarden las consultas; no es una frecuencia exacta garantizada.

### Teclas del panel

| Tecla | Acción |
| --- | --- |
| `q`, `Esc` | Salir |
| `r` | Solicitar una actualización |
| `d` | Mostrar u ocultar detalles |
| `↑` / `↓`, `k` / `j` | Desplazar el contenido |
| `Ctrl+C` | Salir |

El panel pide ampliar la terminal si tiene menos de 36 columnas o 12 filas.
Los porcentajes altos de CPU, RAM y disco aparecen en amarillo desde el 75 % y
en rojo desde el 90 %. La batería usa amarillo al 20 % o menos y rojo al 10 % o menos.

## Cómo funciona el código

Todo el programa está en `src/monitor.py`, dividido en funciones: bloques de
código con nombre que realizan una tarea concreta.

1. **Lectura:** funciones como `battery_info()` y `memory_info()` leen datos de
   Linux; otras consultan `ip`, `nmcli` o `docker`.
2. **Recogida:** `collect_data()` ejecuta las consultas en paralelo y devuelve un
   diccionario: una estructura que relaciona nombres con valores.
3. **Presentación:** `format_snapshot()` convierte ese diccionario en líneas y
   colores, sin volver a consultar el equipo.
4. **Interfaz:** `run_screen()` dibuja el panel con `curses` y atiende las teclas.
   La recogida se ejecuta en segundo plano para no bloquear el teclado. Solo hay
   una muestra en curso; no se acumulan consultas si pulsas `r` varias veces.
5. **Entrada:** `main()` interpreta los argumentos y elige el modo de ejecución.

`snapshot()` combina recogida y presentación para el modo `--once`.

```text
linsockMonitor/
├── README.md
├── src/
│   └── monitor.py
└── tests/
    └── test_monitor.py
```

## Pruebas

Desde la raíz del proyecto:

```bash
python3 -m unittest discover -s tests -v
```

Utilizan `unittest`, incluido en Python. Comprueban cálculos, lectura de archivos
de ejemplo, comandos ausentes o fallidos, presentación y atención del teclado
mientras una consulta sigue pendiente. No necesitan Docker, Wi-Fi ni una terminal
interactiva: sustituyen esas dependencias por datos controlados.

## Limitaciones y resolución de problemas

- **No abre el panel:** utiliza una terminal compatible con `curses`, o ejecuta
  `python3 src/monitor.py --once`. Si falta el módulo `curses`, debes instalar el
  soporte correspondiente de tu distribución, también para usar `--once`.
- **Una consulta tarda:** cada comando externo tiene un límite de tres segundos.
  Se consultan en paralelo, pero no se garantiza una latencia total exacta.
  Al salir puede haber una breve espera hasta que termine la consulta en curso.
- **No aparece la Wi-Fi:** obtener el SSID requiere NetworkManager y `nmcli`.
  No se fuerzan nuevos escaneos. Una conexión gestionada por otro servicio puede
  tener red aunque el SSID no esté disponible.
- **Docker no disponible:** el aviso diferencia programa ausente, falta de
  permisos, espera agotada y problemas de conexión. No se muestran contenedores
  detenidos.
- **CPU:** el porcentaje se calcula con dos lecturas separadas por unos 0,1
  segundos, no como promedio de todo el intervalo de refresco. El número de
  núcleos físicos depende de la información disponible en `/proc/cpuinfo`.
- **Red:** solo se muestra IPv4 local; no la IP pública ni una comprobación de
  acceso a Internet. La interfaz principal se elige por la ruta por defecto y,
  si no está disponible, por una interfaz activa de nombre habitual.
- **RAM y disco:** la RAM usada es el total menos la disponible que informa
  Linux; el disco corresponde únicamente al sistema de archivos que contiene `/`.
- **Contenedores o máquinas virtuales:** los datos dependen de los archivos y
  recursos visibles desde ese entorno; no se garantiza que representen los
  límites de recursos asignados al contenedor.
- **Sin batería:** es normal en un equipo de escritorio o un servidor.

El monitor consulta información: no modifica la configuración del sistema ni
inicia o detiene contenedores.
