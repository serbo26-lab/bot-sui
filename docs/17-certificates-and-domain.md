# Сертификаты и домен

S-UI остается источником сертификата. Bot S-UI проверяет срок действия, fingerprint main и node, предупреждает админа, копирует обновленные cert/key на node и пересобирает remote config.

Смена домена: изменить certificates.domain в config.json, перезапустить bot-sui, открыть preview миграции, проверить изменения и нажать apply.
