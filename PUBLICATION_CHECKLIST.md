# PUBLICATION CHECKLIST

Перед публикацией репозитория на GitHub проверьте этот список.

## Секреты

Убедитесь, что в репозитории нет:

- `config.json`;
- `nodes.json`;
- `database.sqlite`;
- `remote_client_credentials.json`;
- Telegram bot token;
- S-UI token;
- Cloudflare token/global key;
- SSH private keys;
- `fullchain.pem`, `privkey.pem`;
- реальных доменов проекта;
- реальных IP серверов;
- Telegram ID админов;
- логов с персональными данными.

## Example-файлы

В репозитории должны быть только безопасные шаблоны:

- `config.example.json`;
- `nodes.example.json`;
- `.gitignore`;
- документация в `docs/`.

## Проверки кода перед публикацией

Рекомендуемый минимум:

```bash
python3 -m py_compile bot.py
bash -n install.sh
grep -R "REAL_TOKEN\|REAL_IP\|REAL_DOMAIN" .
```

Если проект распространяется одним shell-файлом, проверьте:

```bash
bash -n Stable_*.sh
```

## Документация

Проверьте, что README и docs:

- не обещают готовый SaaS-продукт;
- используют `example.com` и placeholders;
- не содержат внутренние пути с реальными доменами;
- объясняют, что S-UI/sing-box ставится заранее;
- содержат предупреждение про секреты и безопасность.
