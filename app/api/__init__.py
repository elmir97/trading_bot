"""HTTP API Mini App (docs/miniapp-plan.md §2–§4).

Отдельный процесс (uvicorn --factory app.api.main:create_app) из того же образа:
только запросы — без поллинга, планировщика, reconciler и восстановления
открытий (это процесс бота). Торговой логики здесь нет: ввод → ядро
app/execution/opening → ответ.
"""
