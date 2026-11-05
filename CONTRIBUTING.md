# Contribuir a BacklogTalk

Gracias por ayudar a mejorar el proyecto. Son bienvenidos los reportes, las ideas, las mejoras de documentación y los cambios de código, tanto en español como en inglés. Al participar, seguí las [pautas de convivencia](CODE_OF_CONDUCT.md).

Si querés agregar una integración con otro modelo, agente o sistema de tickets, contanos la idea en una incidencia o enviá un pull request. Nos alegra recibir aportes que ayuden a ampliar las posibilidades de BacklogTalk.

## Reportar un problema o proponer una idea

Buscá primero en las incidencias existentes para evitar duplicados. Para un error, indicá qué esperabas, qué pasó, cómo reproducirlo y tu sistema operativo y versión de Python. Para una propuesta, contá qué problema resuelve y cómo imaginás usarla.

Usá datos ficticios y quitá credenciales, nombres de clientes, enlaces privados y texto sensible de capturas y registros. Para vulnerabilidades, seguí la [política de seguridad](SECURITY.md) en lugar de publicarlas en una incidencia.

## Preparar el entorno

1. Hacé un fork y clonalo en tu equipo.
2. Instalá Python 3.12 y las dependencias del sistema indicadas en la [guía](docs/guia.md).
3. Ejecutá `./scripts/install.sh` en Linux o macOS, o `.\scripts\install.ps1` en Windows.
4. Activá el entorno virtual e instalá las herramientas de desarrollo con `python -m pip install -r requirements-dev.txt`.
5. Abrí una rama para tu cambio y ejecutá la aplicación con `./scripts/run.sh` o `.\scripts\run.ps1`.

Los instaladores y la CI verifican el SHA-256 del wheel de pip en `scripts/bootstrap_pip.py` antes de instalarlo. Al actualizar la versión, verificá la URL y el hash del wheel oficial de PyPI y actualizá ambos valores juntos.

## Probar

Ejecutá `ruff check .` y `ruff format --check .` para validar el código.

En Linux, con Xvfb instalado:

```bash
QT_QPA_PLATFORM=offscreen xvfb-run -a .venv/bin/python -m unittest discover -s tests -q
```

En macOS o Windows, con el entorno virtual activado:

```bash
python -m unittest discover -s tests -v
```

Las pruebas existentes utilizan datos simulados y no requieren crear tickets de Jira. Para probar cambios de interfaz, audio o integración, describí qué sistema y dispositivos usaste y qué quedó sin verificar. No incluyas notas, grabaciones, bases de datos ni credenciales reales en las pruebas o en el pull request.

## Enviar un cambio

Explicá el problema, el comportamiento nuevo y cómo lo probaste. Mantené el cambio enfocado y actualizá la documentación cuando cambie el uso. Al contribuir, aceptás que tu aporte se distribuya bajo la [licencia MIT](LICENSE) del proyecto.
