# Guía de BacklogTalk

[← Volver al inicio](../README.md)

## Requisitos

- Python 3.12 con pip. Se recomienda Linux, Windows x86-64 o macOS 12 o superior. Windows puede requerir el runtime de Visual C++.
- PortAudio y un micrófono para grabar. `faster-whisper` descarga el modelo elegido la primera vez que se usa.
- Cursor Agent CLI autenticado o una instalación local de Ollama con un modelo disponible para preparar los borradores. Para Cursor, seguí la [instalación oficial](https://cursor.com/docs/cli/installation).
- [Teamwork Graph CLI (TWG) de Atlassian](https://developer.atlassian.com/cloud/twg-cli/getting-started/installation/) autenticado y con acceso al sitio Jira para consultar metadatos y crear tickets. Tu organización debe permitir los permisos necesarios. La documentación de TWG indica Linux x64; en Windows, BacklogTalk requiere que `twg.exe` esté disponible.

El instalador selecciona las versiones más recientes compatibles con el sistema. En macOS 12 pueden instalarse versiones anteriores de PySide6. Se requiere PyAV 19.0.2 y un FFmpeg con las correcciones de seguridad necesarias; en macOS 12/13 con Apple Silicon no hay ruedas oficiales de PyAV 19.0.2 y puede ser necesario compilarlo con un FFmpeg compatible.

## Instalación

En Linux, instalá las dependencias del sistema necesarias; en Ubuntu pueden incluir `python3-venv` y `libportaudio2`. Si Qt informa que falta el complemento xcb, instalá también `libxcb-cursor0`.

```bash
./scripts/install.sh
./scripts/run.sh
```

En macOS, instalá Python 3.12 y ejecutá `BACKLOGTALK_PYTHON=python3.12 ./scripts/install.sh` y `./scripts/run.sh`. Concedé permiso de micrófono al programa que inicia Python.

En Windows PowerShell, instalá Python 3.12 y ejecutá:

```powershell
.\scripts\install.ps1
.\scripts\run.ps1
```

Si PowerShell bloquea los scripts, ejecutá cada uno con `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` y `powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1`. Concedé permiso de micrófono en la configuración de privacidad de Windows.

Comprobá que `agent --version` y `twg --version` funcionen si usás esas herramientas. Seguí las guías oficiales de [Cursor Agent CLI](https://cursor.com/docs/cli/installation), [Ollama](https://ollama.com/download) y [TWG](https://developer.atlassian.com/cloud/twg-cli/getting-started/installation/) para instalarlas y configurar el acceso. En Configuración, indicá el sitio y proyecto de Jira y elegí el motor de preparación. El paquete `backlogtalk-portable.zip` contiene el código y los lanzadores, pero no Python ni las dependencias; se genera con `python3 scripts/build_portable.py`.

## Uso

1. En **Capturar**, elegí **Micrófono**, **Mic + salida** o **Archivo…**. Al detener la grabación, el audio se guarda y la transcripción comienza en segundo plano. Para reuniones, **Mic + salida** mezcla el micrófono y el audio del equipo; esta fuente requiere Linux, PipeWire y un dispositivo monitor. En Windows y macOS podés grabar el micrófono o importar un archivo.
2. Desde **Historial**, abrí la nota, revisá la transcripción y elegí **Preparar ticket**. Si la transcripción falla o se cancela, el audio se conserva para reintentar.
3. Desde **Tickets**, abrí y revisá el borrador. Elegí proyecto y, si corresponde, épica. Al pulsar **Enviar a Jira**, revisá y confirmá los campos propuestos antes de crear el ticket.
4. En Historial o Tickets podés seleccionar varios elementos con Ctrl o Shift y borrarlos con Supr. Borrar un registro local no elimina el ticket de Jira.

En **Capturar → Destino del ticket → Gestionar**, buscá las épicas accesibles del proyecto en Jira. Marcá las que querés mostrar, editá sus nombres locales si lo necesitás y guardá la selección. Desmarcar una épica solo la quita del selector: no modifica Jira ni borra sus instrucciones.

La bandeja del sistema permite abrir la aplicación y comenzar una grabación; el icono indica cuando el micrófono está activo. Si el sistema no dispone de bandeja, podés minimizar la ventana principal. Al cerrar mientras hay tareas en curso podés esperar o forzar la salida; forzarla puede interrumpir operaciones todavía no guardadas.

La barra inferior muestra el uso total de CPU y memoria del equipo, y los tickets creados y verificados hoy por esta instalación desde que se activó el contador. Para Ollama muestra los tokens de entrada y salida que informó a BacklogTalk; el indicador no incluye otras aplicaciones ni solicitudes sin datos de consumo. Cursor CLI no informa ese dato en la salida utilizada, por eso aparece como no disponible.

Si ocurre un problema de audio, el aviso permite ver y copiar el diagnóstico, que incluye información del sistema, dependencias y formato del audio cuando está disponible. Podés abrir el [repositorio](https://github.com/dertin/backlogtalk) o [reportar un error](https://github.com/dertin/backlogtalk/issues/) desde el menú de la bandeja o **Configuración → Acerca de BacklogTalk**. Antes de publicar el diagnóstico, revisá si contiene rutas u otra información que no quieras compartir.

Si un envío queda en estado incierto, revisá Jira antes de reintentarlo: el ticket podría haberse creado aunque la respuesta no se haya podido verificar. La aplicación no reintenta automáticamente. **Ver salida** conserva el plan del agente y las respuestas de TWG para revisarlos.

## Atajos

En **Configuración → Atajos de la aplicación** podés cambiar las combinaciones de micrófono, reunión y apertura del historial que utiliza la aplicación. Los atajos globales del escritorio se configuran por separado. Para iniciar una grabación mientras está cerrada, configurá un atajo del sistema:

- **Linux/GNOME:** creá un atajo personalizado que ejecute la ruta absoluta de `scripts/run.sh --capture` para el micrófono o `scripts/run.sh --capture-system` para reunión.
- **macOS:** creá una acción rápida que ejecute `scripts/run.sh --capture > /dev/null 2>&1 &` y asignale un atajo.
- **Windows:** creá un acceso directo a `powershell.exe -ExecutionPolicy Bypass -File "RUTA\scripts\run.ps1" --capture` y asignale una tecla en sus propiedades.

Para abrir la aplicación directamente en el historial, configurá un atajo del sistema con `scripts/run.sh --open-history` en Linux o macOS, o con `scripts/run.ps1 --open-history` en Windows. Durante una grabación podés elegir **Cancelar** en la ventana rápida o en la aplicación; tras confirmar, la grabación se descarta sin crear una nota.

Los atajos dependen del escritorio y de sus permisos. Los comandos para macOS y Windows requieren validación en el equipo de destino.

## Modelos, instrucciones y privacidad

En Configuración podés elegir español o inglés y el modelo de `faster-whisper`. La transcripción usa el idioma seleccionado; los modelos `.en` solo admiten inglés. Modelos más grandes pueden requerir más memoria y tiempo. El progreso y el tiempo restante son estimaciones cuando el motor provee información suficiente.

En **Configuración → Instrucciones** podés mantener reglas generales y reglas por sitio, proyecto y épica. Se combinan según el destino al preparar el borrador y los campos de Jira. Indicá los valores obligatorios y, cuando corresponda, los identificadores `customfield_*`; no incluyas credenciales. Jira determina los tipos y campos disponibles y puede rechazar valores o aplicar reglas adicionales.

En **Configuración**, indicá los proyectos que usás y elegí **Sincronizar campos de Jira**. La aplicación guarda los tipos y campos por sitio, proyecto y tipo de ticket. Al preparar una nota, el modelo puede sugerir valores usando la transcripción y las instrucciones; revisalos y modificalos desde **Campos de Jira** en el borrador. Los campos obligatorios aparecen primero y podés ocultar los opcionales. Los valores y las sugerencias se guardan localmente. El origen indicado por el modelo es orientativo y requiere revisión.

La sincronización busca prioridades y equipos accesibles del sitio, y sprints activos o futuros de los tableros del proyecto para mostrarlos en selectores. Los campos de fecha ofrecen un calendario y pueden quedar vacíos. Al enviar, BacklogTalk vuelve a consultar Jira y advierte si un campo o una opción guardada ya no está disponible. Si falla la sincronización, conserva la información anterior e indica el error. Algunos tipos especiales, como campos de aplicaciones, pueden requerir un valor JSON porque TWG no proporciona sus opciones; consultá Jira antes de completarlos. La disponibilidad y las reglas de campos dependen de los permisos y configuración de cada proyecto.

El audio y las transcripciones se guardan localmente. Al elegir Cursor, el texto y las instrucciones necesarias se envían a Cursor. Al elegir Ollama, la aplicación se comunica con el servidor local: los modelos locales procesan el texto en el equipo, mientras que los modelos en la nube de Ollama pueden enviarlo al proveedor. TWG recibe los campos del ticket como argumentos del proceso, que podrían ser visibles para otros usuarios del equipo. La aplicación conserva localmente las respuestas del agente y de TWG.

## Archivos locales

Las rutas predeterminadas son `~/.local/share/backlogtalk` y `~/.config/backlogtalk` en Linux (respetan XDG), `~/Library/Application Support/BacklogTalk` en macOS y `%LOCALAPPDATA%\BacklogTalk` en Windows. **Configuración → Archivos locales** permite abrir las ubicaciones activas y mover los datos. La operación verifica la copia y conserva los originales; no mueve la caché compartida de Hugging Face ni audio externo. La referencia `storage-location.json` permite encontrar la nueva ubicación y no debe borrarse mientras se use.
