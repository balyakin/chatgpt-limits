# ChatGPT Limits

[![Python][badge-python]][python]
[![Platforms][badge-platforms]][quick-start]
[![Codex CLI][badge-codex]][codex]
[![License: MIT][badge-license]][license]

Пятчасовой и недельный лимиты Codex для всех учётных записей ChatGPT Pro — в одном терминале.

С двумя аккаунтами штатный экран уже неудобен: переключения съедают время, а остатки приходится держать в голове.
ChatGPT Limits сводит значения в один экран, регулярно обновляет их и не гасит всю картину из-за сбоя соседнего
аккаунта.

![ChatGPT Limits показывает два аккаунта в терминале](docs/terminal-preview.png)

<sub>Значения на скриншоте демонстрационные. В работе приложение выводит данные, полученные от Codex для ваших аккаунтов.</sub>

## Что здесь важно

- Все аккаунты видны одновременно. Чтобы добавить ещё один, достаточно новой секции в TOML — Python-код не меняется.
- Авторизация остаётся на стороне `codex login`: без паролей в приложении, API-ключей и автоматизации браузера.
- Каждому аккаунту назначается собственный `CODEX_HOME`; параллельный опрос не смешивает сессии.
- Холодного ожидания нет: первый запрос выполняется сразу, следующие — через настроенный интервал.
- Сбой локализуется в блоке конкретного аккаунта. Остальные продолжают обновляться.
- Источник данных — документированный Codex App Server, а не страницы ChatGPT или закрытые HTTP-эндпоинты.
- Runtime ограничен стандартной библиотекой Python 3.12.

## Быстрый старт

Поддерживаются macOS, Linux и Windows. Нужны Python 3.12,
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) и браузер для первого входа. Быстрая проверка — `codex --version`:
если команда отвечает, монитор её увидит. В Windows он сам подхватит `codex.exe` или `codex.cmd` через `PATHEXT`.

В командах ниже используйте `python3.12` на macOS/Linux и `py -3.12` в Windows PowerShell.

1. Скопируйте пример в рабочую конфигурацию:

   macOS/Linux:

   ```shell
   cp config.example.toml config.toml
   ```

   Windows PowerShell:

   ```powershell
   Copy-Item config.example.toml config.toml
   ```

2. Опишите аккаунты в `config.toml`. Поле `slug` задаёт короткий локальный идентификатор, `name` — подпись на экране:

   ```toml
   refresh_seconds = 300

   [[accounts]]
   slug = "personal"
   name = "Personal Pro"

   [[accounts]]
   slug = "work"
   name = "Work Pro"
   ```

3. Один раз авторизуйте каждый аккаунт. Используйте тот же файл конфигурации, с которым затем будете запускать
   монитор:

   ```bash
   python3.12 chatgpt_limits.py --config config.toml --login personal
   python3.12 chatgpt_limits.py --config config.toml --login work
   ```

   Codex откроет официальный вход ChatGPT в браузере. Перед второй командой проверьте, что выбран второй аккаунт.
   Авторизация из `~/.codex` намеренно не переиспользуется — иначе надёжно разделить сессии не получится.

4. Запустите монитор:

   ```bash
   python3.12 chatgpt_limits.py --config config.toml
   ```

Первый экран появится без задержки. Дальше перерисовка идёт каждые `refresh_seconds`; остановка — `Ctrl+C`.

## Как устроена изоляция аккаунтов

Каждый `slug` получает собственный каталог:

```text
~/.chatgpt-limits/
├── accounts/
│   ├── personal/
│   │   ├── auth.json
│   │   └── config.toml
│   └── work/
│       ├── auth.json
│       └── config.toml
└── app.log
```

На Windows `~` — это профиль пользователя, обычно `C:\Users\<имя>`.

Внутренний `config.toml` включает файловое хранилище credentials. Отдельный `CODEX_HOME` направляет каждый процесс
Codex строго в свой каталог. На macOS/Linux приложение зажимает права до `0700` для директорий и `0600` для файлов.
В Windows другая механика: каталог наследует ACL профиля текущего пользователя. Сам монитор в `auth.json` не
заглядывает — с этим файлом работает только Codex CLI.

Рабочий `config.toml` рядом с приложением содержит лишь интервал, `slug` и отображаемые имена. Ни паролей, ни токенов.
Но если сами названия аккаунтов чувствительны, такой файл всё равно не стоит коммитить.

## Откуда берутся числа

Каждый refresh поднимает локальный `codex app-server`, выполняет обязательный handshake и вызывает официальный метод
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt). Монитор берёт из ответа
набор лимитов `codex` и определяет окна по длительности — порядок полей в JSON роли не играет:

- `300` минут соответствуют пятчасовому окну;
- `10080` минут — недельному;
- остаток вычисляется как `100 − usedPercent`;
- `resetsAt` переводится в локальный часовой пояс компьютера.

Нет окна — на экране будет «нет данных». Нет времени сброса — «сброс неизвестен». Старые значения не подставляются,
несуществующие 100% не рисуются.

Codex App Server имеет статус `experimental`, поэтому его контракт способен измениться. После обновления Codex CLI
запустите монитор и убедитесь, что оба окна по-прежнему читаются без `ProtocolError`.

## Конфигурация

| Поле | Что означает |
|---|---|
| `refresh_seconds` | Пауза между завершёнными обновлениями, целое число больше нуля |
| `accounts[].slug` | До 64 символов; начинается со строчной латинской буквы или цифры, дальше допустимы также `_` и `-` |
| `accounts[].name` | Уникальное непустое имя, которое видно в терминале |

Новый аккаунт подключается тремя действиями: добавьте блок `[[accounts]]`, выполните `--login <slug>`, перезапустите
монитор. Если удалить блок, опрос прекратится, но каталог с credentials останется на диске. Это сознательное поведение:
приложение не удаляет пользовательские данные без прямой команды.

## Команды

```text
python3.12 chatgpt_limits.py [--config PATH] [--login SLUG]  # macOS/Linux
py -3.12 chatgpt_limits.py [--config PATH] [--login SLUG]    # Windows
```

| Команда | Результат |
|---|---|
| `python3.12 chatgpt_limits.py` | Запустить монитор с `./config.toml` |
| `python3.12 chatgpt_limits.py --config other.toml` | Взять другой файл конфигурации |
| `python3.12 chatgpt_limits.py --config config.toml --login personal` | Войти в один настроенный аккаунт |
| `python3.12 chatgpt_limits.py --help` | Показать справку |

В Windows замените `python3.12` в таблице на `py -3.12`.

## Если что-то пошло не так

**`Codex CLI is not available in PATH`**

Начните с `codex --version`. Команда не найдена — установите Codex CLI по официальной инструкции и откройте новый
терминал.

На Windows есть развилка. Сам монитор работает нативно, но
[поддержка Windows в Codex CLI](https://help.openai.com/en/articles/11096431) пока экспериментальная; если свежий CLI
всё равно не запускается, используйте WSL2. Это запасной маршрут, а не отдельная версия приложения.

**`Account is not logged in`**

Повторите `--login` для нужного `slug`, обязательно с тем же `--config`, который используется при запуске монитора.

**Один аккаунт показывает ошибку, остальные работают**

Это штатная изоляция сбоя. Подробности записаны в `~/.chatgpt-limits/app.log`; следующий refresh снова опросит
проблемный аккаунт.

**После обновления Codex CLI появился `ProtocolError`**

Найдите точную ошибку в `app.log` и сравните контракт с актуальной документацией
[Codex App Server](https://learn.chatgpt.com/docs/app-server). Если проблема появилась сразу после обновления CLI,
вероятнее всего, изменился experimental-протокол. Исправлять нужно совместимость: обхода через закрытые API у монитора
нет и не планируется.

## Разработка и проверки

Runtime не требует установки пакетов через `pip`. `pytest` нужен только разработчику для запуска тестов:

```bash
python3.12 -m py_compile chatgpt_limits.py
python3.12 -m pytest -q
```

Windows-команды короче не стали, только префикс другой: `py -3.12 -m py_compile chatgpt_limits.py` и
`py -3.12 -m pytest -q`.

Набор тестов проверяет конфигурацию, изоляцию credentials, JSONL-протокол, таймауты, завершение дочерних процессов,
разбор окон, безопасные ошибки, перерисовку терминала и exit codes CLI.

## Границы проекта

ChatGPT Limits показывает ровно те Codex-лимиты, которые App Server вернул для авторизованного ChatGPT-аккаунта. Не
больше. Это не монитор OpenAI Platform Usage и не сводка всех модельных лимитов ChatGPT. Приложение не обращается к
моделям с сообщениями, не расходует и не сбрасывает лимиты, не хранит историю и не маскируется под фоновую службу.

Для проверки деталей обращайтесь к первоисточникам: [Codex App Server](https://learn.chatgpt.com/docs/app-server) и
[хранение учётных данных в Codex](https://learn.chatgpt.com/docs/auth#credential-storage).

[badge-python]: https://img.shields.io/badge/python-3.12-blue
[badge-platforms]: https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-blue
[badge-codex]: https://img.shields.io/badge/Codex%20CLI-required-7c3aed
[badge-license]: https://img.shields.io/badge/license-MIT-green.svg
[python]: https://docs.python.org/3.12/
[quick-start]: #быстрый-старт
[codex]: https://learn.chatgpt.com/docs/codex/cli
[license]: https://github.com/balyakin/chatgpt-limits/blob/main/LICENSE
