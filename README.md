# پیام‌رسان‌های ایرانی برای هرمس (بله + روبیکا)

افزونهٔ (پلاگین) گیت‌وی **Hermes Agent** که دو پیام‌رسان ایرانی — **بله** و **روبیکا** — را به‌عنوان پلتفرم گیت‌وی اضافه می‌کند، دقیقاً مثل تلگرام: ربات یک بار ساخته می‌شود، توکنش را می‌دهید به هرمس، و بعد داخل بله/روبیکا با دستیار هرمس حرف می‌زنید (متن، عکس، فایل، صدا، ویدیو) و می‌توانید اجرای دستورهای شل را با دکمه تأیید کنید.

بدون هیچ وابستگی اضافه: فقط `httpx` که همراه خود هرمس نصب می‌شود.

> English version: [README.en.md](README.en.md)

---

## فهرست

- [ویژگی‌ها](#ویژگیها)
- [پیش‌نیازها](#پیشنیازها)
- [نصب](#نصب)
- [گرفتن توکن ربات](#گرفتن-توکن-ربات)
- [تنظیمات](#تنظیمات) (شامل ویزارد `hermes gateway setup`)
- [جدول متغیرهای محیطی](#جدول-متغیرهای-محیطی)
- [اجرا و استفاده](#اجرا-و-استفاده)
- [امنیت و کنترل دسترسی](#امنیت-و-کنترل-دسترسی)
- [کرون و ارسال بیرونی](#کرون-و-ارسال-بیرونی)
- [دکمه‌ها: تأیید اجرا و پرسش‌ها](#دکمهها-تأیید-اجرا-و-پرسشها)
- [معماری و جزئیات فنی](#معماری-و-جزئیات-فنی)
- [محدودیت‌ها](#محدودیتها)
- [عیب‌یابی](#عیبیابی)
- [پرسش‌های متداول](#پرسشهای-متداول)
- [توسعه و تست](#توسعه-و-تست)
- [ساختار پروژه](#ساختار-پروژه)
- [مجوز](#مجوز)

---

## ویژگی‌ها

| قابلیت | بله | روبیکا |
|---|---|---|
| پیام متنی دوطرفه | ✅ | ✅ |
| دریافت با long-poll (بدون نیاز به HTTPS وب‌هوک) | ✅ | ✅ |
| ارسال عکس / فایل / صدا / ویدیو | ✅ | ✅ |
| دریافت عکس / فایل / صدا / ویدیو (دانلود و کش خودکار) | ✅ | ✅ |
| دکمه برای تأیید اجرای دستور و پرسش‌های clarify | ✅ (کیبورد شیشه‌ای/inline) | ✅ (کیبورد چت/keypad) |
| پاسخ تایپی به‌جای دکمه (شماره گزینه) | ✅ | ✅ |
| تقسیم خودکار پیام‌های بلند | ✅ | ✅ |
| وایت‌لیست کاربر + DM pairing | ✅ | ✅ |
| کانال خانه برای تحویل نتیجهٔ کرون (`deliver=bale/rubika`) | ✅ | ✅ |
| حذف/ویرایش پیام | ✅ (editMessageText) | ✅ (editMessageText / deleteMessage) |
| نشانگر «در حال تایپ» | ❌ (API ندارد) | ❌ (API ندارد) |

نکتهٔ مهم: هر دو پلتفرم از یک الگوی شبیه تلگرام استفاده می‌کنند — `POST` با بدنهٔ JSON به متدهای `getUpdates` / `sendMessage` / `getFile` — پس این افزونه از همان منطق آداپتر تلگرام هرمس پیروی می‌کند: مدیا دوطرفه، وایت‌لیست، کانال خانهٔ کرون، دکمه‌های تأیید و بخش‌بندی پیام.

---

## پیش‌نیازها

- **Hermes Agent** نصب‌شده و قابل اجرا (`hermes --version`).
- پایتون ۳٫۱۰+ (همان مفسری که هرمس با آن اجرا می‌شود).
- `httpx` — از قبل همراه هرمس نصب است، پس چیزی برای نصب اضافه لازم نیست.
- یک ربات در بله و/یا روبیکا + توکن آن.

---

## نصب

### روش ۱ — نصب از گیت‌هاب (پیشنهادی)

```bash
hermes plugins install https://github.com/<username>/hermes_iran_messengers --enable
```

### روش ۲ — نصب دستی (کپی پوشه)

```bash
git clone https://github.com/<username>/hermes_iran_messengers
cp -r hermes_iran_messengers ~/.hermes/plugins/
hermes plugins enable iran-messengers
```

سپس بررسی کنید که افزونه دیده شده است:

```bash
hermes plugins list
hermes plugins show iran-messengers
```

> ⚠️ **نام پوشه را با زیرخط (`_`) نگه دارید.** پوشهٔ افزونه خودش یک پکیج پایتون است (فایل `__init__.py` دارد)؛ اگر نام پوشه خط تیره داشته باشد، اجرای تست‌ها با pytest شکست می‌خورد (خود هرمس با خط تیره مشکلی ندارد، فقط pytest). پس اگر مخزن را با خط تیره کلون کردید و می‌خواهید تست بزنید، نام پوشه را به `hermes_iran_messengers` تغییر دهید.

> ℹ️ **برای بازبین‌ها:** `hermes plugins validate .` روی این افزونه پاس می‌شود، ولی اسکن امنیتی برای متن‌های فارسی فقط یک «احتیاط» می‌دهد: `invisible_unicode` از نوع نیم‌فاصله (ZWNJ، `U+200C`) که در نگارش درست فارسی لازم است. کدها، `plugin.yaml` و `.env.example` هیچ کاراکتر نامرئی ندارند (یک تست هم همین را تضمین می‌کند: `tests/test_registration.py::test_plugin_yaml_has_no_invisible_unicode`).

### حذف افزونه

```bash
hermes plugins disable iran-messengers
hermes plugins remove iran-messengers
```

---

## گرفتن توکن ربات

### بله

1. در بله به‌سراغ ربات **BotFather** بروید (در مستندات بله توضیح داده شده: <https://docs.bale.ai/>).
2. با `/newbot` یک ربات بسازید و نام و یوزرنیم انتخاب کنید.
3. توکنی که می‌دهد را کپی کنید — می‌شود `BALE_BOT_TOKEN`.

### روبیکا

1. در روبیکا ربات **BotFather** را باز کنید (مستندات: <https://rubika.ir/botapi>).
2. ربات بسازید و توکن بگیرید — می‌شود `RUBIKA_BOT_TOKEN`.
3. در تنظیمات حریم خصوصی ربات، اجازهٔ پیام‌گیری در گروه (در صورت نیاز) را بدهید.

توکن‌ها را **هیچ‌وقت** در متن چت، اسکرین‌شات یا کد نگذارید. فقط در `~/.hermes/.env` یا بخش `config.yaml`.

---

## تنظیمات

سه راه (هر کدام راحت‌تر بود):

### ۰) ویزارد `hermes gateway setup` (ساده‌ترین راه)

```bash
hermes gateway setup        # از منوی پلتفرم‌ها «Bale (بله)» یا «Rubika (روبیکا)» را انتخاب کنید
```

ویزارد خودش توکن را می‌پرسد، آن را **روی API واقعی تست می‌کند** (اگر معتبر نبود هشدار می‌دهد و اجازه می‌دهد بازهم ذخیره کنید)، لیست کاربران مجاز و «کانال خانه» را تنظیم می‌کند و در پایان می‌گوید گیتوی را ری‌استارت کنید. بعد از این دستور، مقدارها در `~/.hermes/.env` نوشته می‌شوند.

> ⚠️ اگر نسخهٔ افزونه‌ای که نصب کرده‌اید `wizard.py` ندارد (نسخه‌های قدیمی‌تر از این مخزن)، انتخاب بله/روبیکا در ویزارد فقط یک خط راهنما چاپ می‌کند و به همان منوی انتخاب برمی‌گردد (شبیه «ریلود شدن صفحه»). با `git pull` + کپی دوبارهٔ پوشه در `~/.hermes/plugins/` مشکل رفع می‌شود؛ جزئیات در `CHANGELOG.md`.

### الف) فایل محیطی (ساده‌ترین بدون ویزارد)

```bash
hermes config set BALE_BOT_TOKEN "123456:AAA..."     # یا دستی در ~/.hermes/.env
hermes config set RUBIKA_BOT_TOKEN "ABC..."
```

یا مستقیم در `~/.hermes/.env`:

```dotenv
BALE_BOT_TOKEN=123456:AAA...
RUBIKA_BOT_TOKEN=ABC...
BALE_ALLOWED_USERS=123456789            # آی‌دی عددی کاربران مجاز، با کاما
BALE_HOME_CHANNEL=123456789             # کانال خانه برای نتیجهٔ کرون
RUBIKA_ALLOW_ALL_USERS=false
```

### ب) فایل `config.yaml`

هر دو شکل نوشتن پشتیبانی می‌شود (کلیدها هم مستقیم زیر نام پلتفرم، هم داخل `extra`):

```yaml
platforms:
  bale:
    enabled: true
    extra:
      token: "123456:AAA..."
      api_base: "https://tapi.bale.ai"   # فقط اگر پراکسی/آینه دارید
      markdown: false                    # پیش‌فرض: متن ساده
      poll_timeout: 25
  rubika:
    enabled: true
    extra:
      token: "ABC..."
      api_base: "https://botapi.rubika.ir/v3"
      poll_interval: 3
      interactive: true                  # دکمه‌های کیبورد برای تأیید/پرسش
```

مقدار متغیر محیطی همیشه بر `config.yaml` اولویت دارد.

### ج) ویزارد / داشبورد

```bash
hermes gateway setup          # انتخاب بله یا روبیکا و وارد کردن توکن
```
یا در داشبورد هرمس (<http://127.0.0.1:9119>) تب **Messaging**.

---

## جدول متغیرهای محیطی

### مشترک (برای هر پلتفرم با پیشوند خودش)

| متغیر | معنا | پیش‌فرض |
|---|---|---|
| `<PREFIX>_BOT_TOKEN` | توکن ربات (اجباری) | — |
| `<PREFIX>_ALLOWED_USERS` | آی‌دی کاربران مجاز، جدا با کاما | خالی (دسترسی بسته) |
| `<PREFIX>_ALLOW_ALL_USERS` | اجازه به همه (فقط توسعه/تست) | `false` |
| `<PREFIX>_HOME_CHANNEL` | چت پیش‌فرض برای نتیجهٔ کرون و اعلان‌ها | خالی |
| `<PREFIX>_HOME_CHANNEL_NAME` | نام نمایشی کانال خانه | `Home` |
| `<PREFIX>_API_BASE` | آدرس پایهٔ API (برای پراکسی/خودمیزبان) | مقدار پیش‌فرض پلتفرم |
| `<PREFIX>_MAX_MESSAGE_LENGTH` | سقف طول هر پیام برای بخش‌بندی | `4096` |

که `<PREFIX>` یکی از `BALE` یا `RUBIKA` است.

### مخصوص بله

| متغیر | معنا | پیش‌فرض |
|---|---|---|
| `BALE_MARKDOWN` | ارسال با مارک‌داون `*bold*` بله (ناپایدار) | `false` (متن ساده) |
| `BALE_POLL_TIMEOUT` | زمان انتظار long-poll (ثانیه) | `25` |

### مخصوص روبیکا

| متغیر | معنا | پیش‌فرض |
|---|---|---|
| `RUBIKA_POLL_INTERVAL` | فاصلهٔ بین درخواست‌های getUpdates (ثانیه) | `3` |
| `RUBIKA_INTERACTIVE` | نمایش دکمه‌های کیبورد برای تأیید/پرسش | `true` |

---

## اجرا و استفاده

```bash
hermes gateway run        # اجرای پیش‌زمینه (برای WSL/داکر/Termux پیشنهاد می‌شود)
hermes gateway install    # نصب به‌عنوان سرویس systemd/launchd
hermes gateway start|stop|restart|status
hermes gateway setup      # تنظیم پلتفرم‌ها
```

بعد از اتصال، داخل بله/روبیکا به ربات پیام بدهید؛ هر پیام به‌عنوان یک نشست هرمس پردازش می‌شود (مثل تلگرام). دستورهای اسلش هرمس (`/status`, `/model`, ...) هم کار می‌کنند.

ارسال بدون اجرای گیت‌وی (ربات‌توکن‌ها) هم ممکن است:

```bash
hermes send --to bale:123456789 "سلام از طرف اسکریپت"
hermes send --to rubika:u0ABC... "نتیجهٔ بیلد: موفق"
hermes send --list | grep -E 'bale|rubika'
```

---

## امنیت و کنترل دسترسی

پیش‌فرض امن است: **تا وقتی وایت‌لیست پر نشده یا DM pairing تأیید نشود، هیچ‌کس نمی‌تواند از ربات استفاده کند.**

- وایت‌لیست: `BALE_ALLOWED_USERS=111,222` و/یا `RUBIKA_ALLOWED_USERS=...`
- افتتاح کامل (فقط محیط تست):

```bash
hermes config set BALE_ALLOW_ALL_USERS true     # ⚠️ همه می‌توانند ربات را صدا بزنند
```

- مدیریت درخواست‌های دسترسی:

```bash
hermes pairing list
hermes pairing approve <platform> <request-id|code>
hermes pairing revoke <platform> <user-id>
```

- تأیید اجرای دستورهای حساس (`rm -rf`, ...) به‌صورت دکمه‌ای در چت انجام می‌شود و فقط برای کاربران مجاز پذیرفته می‌شود.

---

## کرون و ارسال بیرونی

1. یک بار داخل چت دلخواه بنویسید تا آی‌دی چت مشخص شود، بعد:

```bash
hermes config set BALE_HOME_CHANNEL <chat_id>
hermes config set RUBIKA_HOME_CHANNEL <chat_id>
```

2. سپس در کرون‌جاب‌ها `deliver=bale` یا `deliver=rubika` بدهید (کرون با `standalone_sender_fn` این افزونه مستقل از گیت‌وی هم می‌فرستد):

```bash
hermes cron add --schedule "0 9 * * *" --deliver bale "خلاصهٔ امروز را بفرست"
```

---

## دکمه‌ها: تأیید اجرا و پرسش‌ها

وقتی هرمس برای اجرای یک دستور از شما تأیید می‌خواهد، پیام با دکمه می‌آید:

- **بله:** کیبورد شیشه‌ای (inline keyboard) با دکمه‌های «Approve once / Always / Deny»؛
- **روبیکا:** کیبورد چت (chat keypad) تک‌باره زیر باکس متن.

در هر دو حالت **پاسخ تایپی هم معتبر است** (شمارهٔ گزینه یا متن گزینه)، پس اگر دکمه کار نکرد همین راه هست. تأییدها فقط از کاربران مجاز پذیرفته می‌شود و پیام دکمه پس از پاسخ، ویرایش/پاک‌سازی می‌شود.

---

## معماری و جزئیات فنی

```
پیام ورودی                دستیار / ابزارها                 پیام خروجی
   │                             │                              ▲
   ▼                             ▼                              │
اسکرول long-poll ──► MessageEvent ──► هرمس (نشست، مدل، ابزار) ──┴──► sendMessage/sendFile
```

### بله

- پایه: `https://tapi.bale.ai/bot<token>/<Method>`، بدنهٔ JSON، پاسخ `{"ok": true, "result": ...}`.
- دریافت: `getUpdates` با `offset` و long-poll (پیش‌فرض ۲۵ ثانیه)؛ هر `update_id` بعد از پردازش ack می‌شود تا پیام تکرار نشود.
- مدیا: `getFile` → `file_path` → دانلود از `https://tapi.bale.ai/file/bot<token>/<file_path>` و کش محلی.
- ارسال مدیا: `sendPhoto` / `sendDocument` / `sendVoice` / `sendVideo` با آپلود multipart.
- دکمه‌ها: `reply_markup.inline_keyboard` و پاسخ کاربر به‌شکل `callback_query` که با `answerCallbackQuery` تأیید می‌شود.
- متن: پیش‌فرض متن ساده؛ اگر `BALE_MARKDOWN=true` باشد با `parse_mode=Markdown` ارسال می‌شود و **در صورت خطای پارس، خودکار همان پیام به‌صورت متن ساده دوباره فرستاده می‌شود** (مارک‌داون بله به فاصله دور ستاره‌ها حساس است و ناپایدار).

### روبیکا

- پایه: `https://botapi.rubika.ir/v3/<token>/<Method>`، پاسخ `{"status": "OK", "data": {...}}`.
- دریافت: `getUpdates` با `offset_id` (پولینگ هر ۳ ثانیه به‌صورت پیش‌فرض، چون long-poll ندارد) و `next_offset_id` برای صفحهٔ بعد.
- نوع آپدیت‌ها: `NewMessage`, `UpdatedMessage`, `RemovedMessage`, `StartedBot`, `StoppedBot`.
- نوع چت (خصوصی/گروه/کانال) در خود آپدیت نیست، پس یک بار `getChat` زده و نتیجه برای هر چت کش می‌شود.
- مدیا: `getFile(file_id)` → `download_url` → دانلود و کش.
- ارسال مدیا: `requestSendFile(type)` → گرفتن `upload_url` → آپلود multipart → `sendFile(chat_id, file_id, text)`. محدودیت‌ها: تصویر ≤ ۱۰ مگابایت، فایل/ویدیو ≤ ۵۰ مگابایت.
- دکمه‌ها: چون روبیکا «inline keyboard» ندارد، از **chat keypad** استفاده می‌کنیم: با `sendMessage` + `chat_keypad` (کلید `one_time_keyboard`) و شناسهٔ دکمه‌ای که خودمان می‌سازیم (`hz:<token>`). فشردن دکمه به‌شکل `NewMessage` با `aux_data.button_id` برمی‌گردد؛ معنی دکمه از نقشهٔ داخلی بازیابی و به همان `resolve_gateway_approval` / `resolve_gateway_clarify` هرمس داده می‌شود و در پایان کیبورد با `editChatKeypad(Remove)` جمع می‌شود.
- متن: روبیکا فقط متن ساده رندر می‌کند (استایل نیاز به API متادیتا دارد)، پس مارک‌داون پیش از ارسال **حذف** می‌شود تا کاربر ستاره و بک‌تیک نبیند.

### فایل‌ها

| فایل | نقش |
|---|---|
| `plugin.yaml` | مانیفست: نام، توکن‌های لازم، متغیرهای اختیاری، `python_dependencies` |
| `__init__.py` | نقطهٔ ورود پلاگین → `adapter.py` |
| `adapter.py` | `register(ctx)` — ثبت هر دو پلتفرم در رجیستری هرمس |
| `bale.py` | آداپتر کامل بله (`BaleAdapter`) |
| `rubika.py` | آداپتر کامل روبیکا (`RubikaAdapter`) |
| `common.py` | بخش مشترک: حلقهٔ پولینگ با backoff، بخش‌بندی متن، تمیزکاری مارک‌داون، دانلود مدیا |
| `tests/` | ۵۳ تست با سرور جعلی API (بدون شبکهٔ واقعی) |

---

## محدودیت‌ها

- **بدون نشانگر تایپینگ**: هیچ‌کدام از دو API متد chat action ندارند؛ پس کاربر «در حال نوشتن…» نمی‌بیند.
- **بدون ترد/تاپیک**: مثل تلگرام بدون گروه تاپیک‌دار؛ هر چت یک نشست.
- **استریم قابل‌دیدن نیست**: پیام‌های بلند بعد از کامل شدن (یا در تکه‌ها) ارسال می‌شوند.
- **مارک‌داون بله** ناپایدار است؛ پیش‌فرض متن ساده. اگر لازم است فقط `*bold*` و `` `code` `` استفاده کنید (جدول و تیتر ترجمه نمی‌شود).
- **روبیکا استایل ندارد**؛ خروجی همیشه متن ساده است.
- **حالت ویرایش پیام**: روبیکا آپدیت `UpdatedMessage` می‌فرستد ولی هرمس آن را به‌عنوان پیام جدید پردازش نمی‌کند (ویرایش‌ها نادیده گرفته می‌شوند تا پاسخ تکراری ساخته نشود).
- **محدودیت حجم مدیا** در روبیکا (۱۰MB/۵۰MB) و در بله (صدا باید OGG/Opus زیر ۱ مگابایت باشد).
- اگر روی شبکهٔ شما دسترسی به `tapi.bale.ai` یا `botapi.rubika.ir` محدود است، از `*_API_BASE` برای پراکسی/آینه استفاده کنید.

---

## عیب‌یابی

| نشانه | علت و درمان |
|---|---|
| `getMe` رد می‌شود / `token rejected` | توکن غلط یا باطل است؛ توکن را از BotFather دوباره بگیرید. |
| گیت‌وی می‌گوید پلتفرم enable نیست | توکن در `~/.hermes/.env` نبوده؛ `hermes config set BALE_BOT_TOKEN ...` و ری‌استارت گیت‌وی. |
| ربات جواب نمی‌دهد ولی پیام می‌رسد | کاربر در وایت‌لیست نیست؛ `hermes pairing list` یا `BALE_ALLOWED_USERS`. |
| خطای «can't parse entities» در بله | مارک‌داون فرستاده شده؛ `BALE_MARKDOWN=false` کنید (افزونه خودش هم fallback دارد). |
| `429 / TOO_REQUESTS` | محدودیت نرخ پلتفرم؛ افزونه با backoff دوباره تلاش می‌کند. |
| مدیا دانلود نمی‌شود | `getFile` ناموفق یا حجم بیش از حد؛ لاگ گیت‌وی را ببینید. |
| دکمه‌ها نمی‌آیند (روبیکا) | `RUBIKA_INTERACTIVE=false` است؛ true کنید یا با شمارهٔ گزینه تایپ کنید. |
| تست‌ها جمع نمی‌شوند (`attempted relative import`) | نام پوشهٔ مخزن خط تیره دارد؛ به `hermes_iran_messengers` تغییر نام دهید. |

لاگ‌ها:

```bash
hermes gateway status
journalctl --user -u hermes-gateway -f     # اگر به‌صورت سرویس نصب شده
hermes gateway run                          # اجرای پیش‌زمینه و دیدن لاگ زنده
```

---

## پرسش‌های متداول

**۱. آیا وب‌هوک و HTTPS لازم است؟**
نه. هر دو پلتفرم با polling کار می‌کنند؛ همین باعث می‌شود روی سرور خانگی/بدون دامنه هم راحت اجرا شود.

**۲. آیا می‌توانم هر دو را هم‌زمان فعال کنم؟**
بله، افزونه دو پلتفرم مستقل (`bale` و `rubika`) ثبت می‌کند؛ می‌توانید فقط یکی را هم فعال کنید (توکن دومی را خالی بگذارید).

**۳. تفاوت با تلگرام چیست؟**
از دید کاربر تقریباً هیچ: دستورها، دکمه‌ها، مدیا و وایت‌لیست یکسان‌اند. تنها تفاوت‌ها: نبود تایپینگ/تاپیک و ضعف مارک‌داون بله.

**۴. رمزها را کجا بگذارم؟**
فقط `~/.hermes/.env` یا `config.yaml`. توکن را در چت/کد/ریپو قرار ندهید.

**۵. افزونه روی نسخه‌های قدیمی هرمس کار می‌کند؟**
از APIهای پلاگین `register_platform` استفاده می‌کند. اگر `hermes plugins validate .` خطایی داد، هرمس را به‌روزرسانی کنید.

---

## توسعه و تست

```bash
cd hermes_iran_messengers
~/.hermes/hermes-agent/venv/bin/python -m pytest        # ۵۳ تست
hermes plugins validate .                                # اعتبارسنجی مانیفست و لود
hermes plugins doctor .                                  # بررسی با قراردادهای واقعی ران‌تایم
```

### تست دودی (end-to-end با ماشین‌آلات واقعی هرمس)

`scripts/smoke_test.py` افزونه را از مسیر واقعی هرمس بالا می‌آورد — discovery از یک `HERMES_HOME` موقت، رجیستری، `create_adapter`، اتصال، ارسال، دریافت رویداد در هندلر گیت‌وی و ارسال کرون/مستقل — همه روی یک سرور جعلی محلی و بدون هیچ توکن واقعی:

```bash
# یک HOME موقت بسازید، افزونه را همان‌جا کپی و فعال کنید
export SMOKE=/tmp/hermes_smoke
mkdir -p $SMOKE/plugins && cp -r . $SMOKE/plugins/hermes_iran_messengers
HERMES_HOME=$SMOKE hermes plugins enable iran-messengers

HERMES_HOME=$SMOKE ~/.hermes/hermes-agent/venv/bin/python scripts/smoke_test.py
# → SMOKE OK — discovery + registry + adapter + wire + cron sender verified for: bale, rubika
```

تست‌ها هیچ شبکهٔ واقعی یا توکنی لازم ندارند: یک سرور جعلی `aiohttp` نقش API هر دو پلتفرم را بازی می‌کند (دریافت/ارسال، آپلود و دانلود فایل، دکمه‌ها، خطاها) و همهٔ فراخوانی‌ها برای بررسی روی سیم ثبت می‌شوند. `pytest-asyncio` لازم نیست (تست‌های async با `asyncio.run` اجرا می‌شوند).

> نکته: تست‌های آداپتر `gateway.*` را از نصب هرمس import می‌کنند، پس **برای اجرای کامل مجموعه نیاز به یک نصب هرمس دارید**. اگر هرمس در محیط شما نصب نباشد، workflow گیت‌هاب (`static-checks`) فقط بررسی‌های استاتیک را انجام می‌دهد و اجرای تست‌ها را با پیام واضح رد می‌کند (نه اینکه وانمود کند تست‌ها پاس شده‌اند).

> فرمان‌ها را از ریشهٔ مخزن و بدون آرگومان اجرا کنید (`python -m pytest`)، چون پوشهٔ `tests/` در `pytest.ini` تعریف شده است.

برای افزودن قابلیت جدید: منطق مشترک را در `common.py` بگذارید و آداپتر را کوچک نگه دارید؛ برای هر رفتار جدید یک تست با سرور جعلی بنویسید.

---

## ساختار پروژه

```
hermes_iran_messengers/
├── plugin.yaml            # مانیفست افزونه
├── __init__.py            # نقطهٔ ورود → register
├── adapter.py             # ثبت پلتفرم‌ها (register(ctx))
├── bale.py                # آداپتر بله
├── rubika.py              # آداپتر روبیکا
├── common.py              # بخش مشترک دو آداپتر
├── wizard.py              # جریان تعاملی `hermes gateway setup` (setup_fn)
├── scripts/
│   └── smoke_test.py      # تست دودی end-to-end روی ماشین خودتان
├── pytest.ini
├── tests/
│   ├── conftest.py        # HERMES_HOME موقت، سرور جعلی، اجرای تست‌های async
│   ├── test_common.py
│   ├── test_bale.py
│   ├── test_rubika.py
│   ├── test_wizard.py     # ویزارد ستاپ (setup_fn، پرسش‌ها، ذخیره‌سازی)
│   └── test_registration.py
├── README.md              # همین فایل (فارسی)
└── README.en.md           # English
```

---

## منابع

- مستندات بله: <https://docs.bale.ai/> — API: `https://tapi.bale.ai` (swagger: `/swagger/swagger.json`)
- مستندات روبیکا: <https://rubika.ir/botapi> و <https://rubika.ir/botapi/methods> و <https://rubika.ir/botapi/models>
- هرمس (Hermes Agent): <https://hermes-agent.nousresearch.com/docs> — راهنمای افزودن پلتفرم: `gateway/platforms/ADDING_A_PLATFORM.md` در سورس هرمس
- کتابخانهٔ مرجع پایتون روبیکا: <https://github.com/rubika-bot-api/rubika_bot_api>

## مجوز

MIT — ببینید [LICENSE](LICENSE).
