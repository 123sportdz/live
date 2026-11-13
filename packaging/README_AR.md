# التشغيل كخدمة

## Linux (systemd، وضع المستخدم)

```bash
mkdir -p ~/.config/systemd/user
cp packaging/live-relay.service ~/.config/systemd/user/live-relay.service
# عدّل المسار داخل الملف إذا لم يكن المشروع في ~/live-relay
systemctl --user daemon-reload
systemctl --user enable --now live-relay
loginctl enable-linger "$USER"   # ليعمل بعد إعادة التشغيل دون تسجيل دخول
systemctl --user status live-relay
journalctl --user -u live-relay -f
```

## Windows (مهمة مجدولة)

شغّل `start_live_relay.bat` مرة واحدة لإنشاء `.venv`، ثم:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\install-windows-task.ps1
```

تظهر المهمة باسم **Live Relay** في Task Scheduler، وتبدأ عند تسجيل الدخول،
وتُعاد تلقائياً عند خروج العملية. أوقفها من Task Scheduler أو بـ
`Stop-ScheduledTask -TaskName "Live Relay"`.

> في الوضعين، يعمل المشرف الداخلي (supervisor) على إعادة تشغيل المُرحّل عند
> الأعطال غير المتوقعة، ويكتب نبضة في `heartbeat.json` كل 15 ثانية.

## CI على GitHub

تطبيق GitHub المستخدم للرفع لا يملك صلاحية `workflows`، لذلك لا يستطيع إنشاء
`.github/workflows/ci.yml` داخل المستودع. الملف موجود هنا باسم `packaging/ci.yml`.

لتفعيله مرة واحدة: افتح المستودع على GitHub ← **Add file → Create new file** ←
الاسم `.github/workflows/ci.yml` ← الصق محتوى `packaging/ci.yml` ← Commit.
أو من جهازك بحسابك:

```bash
mkdir -p .github/workflows
cp packaging/ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: add workflow" && git push
```
