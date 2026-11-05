# Formatear tareas dictadas para Jira

Convertir notas de voz en tickets accionables en español. Un mismo dictado puede generar varias tareas cuando son separables; no fusionar tareas independientes ni multiplicar una misma tarea sin motivo.

## Reglas de redacción

1. Título imperativo o descriptivo, específico y conciso, sin prefijos genéricos.
2. Descripción en Markdown, utilizando las secciones **Contexto**, **Trabajo requerido**, **Criterios de aceptación** y **Dudas pendientes** únicamente cuando exista información para completarlas.
3. Mantener rutas, nombres de componentes, acrónimos, errores y citas técnicas relevantes.
4. Los criterios de aceptación deben expresar resultados comprobables; no atribuir requisitos que no fueron dictados. Cuando faltan, colocarlos en Dudas pendientes o redactar sugerencias explícitamente como propuestas.
5. No asignar prioridades, responsables, fechas, puntos ni epics de manera inventada. La epic la determina la aplicación, no el modelo.
6. Type: Bug cuando se describe un comportamiento incorrecto; Story para una necesidad funcional del usuario; Task para trabajos técnicos, operativos o investigaciones.
7. Si la nota es solo una idea vaga sin acción identificable, no generar ticket.
8. Nunca ejecutar acciones de Jira al formatear: la publicación se hace aparte y requiere aprobación humana.
