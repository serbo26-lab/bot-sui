# Recovery procedures

## Stars payment is pending

Open Admin -> Payments, find the payment. If the user did not pay, wait for TTL auto-cancel or cancel it manually from the user payment screen.

## Payment is apply_failed

This means Telegram/payment side succeeded, but S-UI application failed or was interrupted.

1. Check S-UI availability.
2. Open Admin -> Payments -> the payment.
3. Press `♻️ Повторить применение`.
4. Verify the user received the subscription/renewal message.

## Manual issue without payment

Use only for admin recovery or compensation. The payment receives `provider_charge_id=admin_manual:<admin_id>`.

## Logs

```bash
journalctl -u bot-sui -n 200 --no-pager
tail -n 200 /opt/bot-sui/logs/bot.log
```
