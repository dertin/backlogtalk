# Política de seguridad

## Reportar una vulnerabilidad

No publiques detalles sensibles, credenciales ni una prueba de explotación en una incidencia pública. Si el repositorio ofrece **Security → Advisories → Report a vulnerability**, enviá el informe por ese canal privado. Si no está disponible, abrí una incidencia sin detalles sensibles para solicitar una vía privada de contacto.

Incluí la versión, los pasos para reproducir, el impacto y las condiciones necesarias, usando datos ficticios. No hay un plazo de respuesta garantizado.

## Alcance y límites de confianza

BacklogTalk es una aplicación de escritorio que procesa audio y notas locales, consulta modelos y utiliza Cursor, Ollama y TWG para preparar o publicar tickets. Audio y video importados, texto de transcripciones, respuestas de modelos y de herramientas externas, y datos de Jira deben tratarse como entradas no confiables. Las notas, grabaciones, configuración y respuestas guardadas pueden contener información sensible.

## Propiedades que deben mantenerse

- La aplicación debe solicitar confirmación antes de crear un ticket y evitar reintentos automáticos cuando el resultado sea ambiguo.
- Las operaciones sobre archivos y datos locales deben respetar el destino seleccionado y evitar borrar o sobrescribir archivos no administrados.
- Las entradas externas no deben convertirse en comandos de shell ni modificar las instrucciones del sistema sin autorización.
- Los errores y diagnósticos no deben exponer credenciales innecesariamente.

## Evaluación

Reportá problemas con impacto y una ruta de reproducción plausibles, incluidos los que afecten a configuraciones de desarrollo o pruebas. La aplicabilidad y la gravedad dependen de las condiciones reales; no se establecen exclusiones ni riesgos aceptados generales. Las dependencias, la visibilidad de argumentos de procesos y las integraciones externas requieren evaluación en su contexto.
