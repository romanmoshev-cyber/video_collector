# Video Collector v3 (Telethon + Aiogram)

Крутой “боевой” бот с логированием и понятным прогрессом.

## Что умеет
- Сканирует **все** диалоги аккаунта: лички, группы, супергруппы, каналы (через MTProto Telethon).
- Ищет подходящие видео и **форвардит** в @Content_Vertical_BOT.
- Управление через control-bot (Bot API): меню, выбор чатов, период, порядок, стоп.
- Показ прогресса: сколько чатов обработано, сколько сообщений проверено, сколько видео подошло и сколько форварднуло.
- Логи в консоль и файл `logs/app.log` (ротируются).

## Фильтр видео
- Вертикальное: h > w
- Длительность: >= 180 секунд
- Ширина: w >= 900
- Размер: >= 10 МБ на минуту (пропорционально)

## Windows PowerShell
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python main.py
```

## Логи
- `logs/app.log`
- смотреть “хвост”:
```powershell
Get-Content .\logs\app.log -Tail 200 -Wait
```

## Важно
Перед первым запуском открой диалог с @Content_Vertical_BOT и нажми Start (чтобы форвард был разрешён).
