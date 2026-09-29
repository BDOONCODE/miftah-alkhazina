# نشر مفتاح الخزينة (مجانًا)

ثلاث خدمات مجانية:

- **GitHub:** يحفظ الكود.
- **Supabase:** قاعدة البيانات.
- **Render:** يشغّل الموقع ويعطيه رابط.

> ⚠️ للعرض والتجربة فقط. خوادم Supabase وRender ما هي داخل السعودية، فلا تدخل بيانات عملاء حقيقية قبل مراجعة متطلبات حماية البيانات.

## ١. GitHub: رفع الكود

1. سجّل في https://github.com (مجاني).
2. من زر **+** فوق، اختر **New repository**.
   - الاسم: `miftah-alkhazina`
   - اختر **Private** (خاص).
   - لا تضيف README ولا .gitignore.
3. انسخ رابط المستودع، مثل `https://github.com/USERNAME/miftah-alkhazina.git`، وأرسله لـClaude عشان يرفع الكود.

## ٢. Supabase: قاعدة البيانات

1. سجّل في https://supabase.com (مجاني).
2. اضغط **New project**:
   - الاسم: `miftah-alkhazina`
   - **Database password:** اضغط Generate، وانسخها واحفظها عندك.
   - **Region:** اختر **Central EU (Frankfurt)**.
3. بعد ما يجهز المشروع (دقيقتين تقريبًا)، اضغط زر **Connect** فوق.
4. اختر **Session pooler** (مهم: مو Direct connection، لأن Render ما يدعمه) وانسخ الرابط. شكله كذا:
   `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres`
5. استبدل `[YOUR-PASSWORD]` بكلمة مرور القاعدة من الخطوة ٢.

> 🔒 هذا الرابط فيه كلمة مرور القاعدة. **لا ترسله في المحادثة.** الصقه بنفسك في Render في الخطوة الجاية.

## ٣. Render: تشغيل الموقع

1. سجّل في https://render.com بحساب GitHub نفسه.
2. من **New +** اختر **Blueprint**، واختر مستودع `miftah-alkhazina`. Render يقرأ ملف `render.yaml` تلقائيًا.
3. بيطلب منك أربع قيم:

   | المتغير | وش تحط فيه |
   |---|---|
   | `DATABASE_URL` | رابط Supabase من الخطوة ٢ (مع كلمة المرور) |
   | `ADMIN_USERNAME` | اسم دخول المدير، مثل `admin` |
   | `ADMIN_PASSWORD` | كلمة مرور مؤقتة للمدير (٨ أحرف على الأقل). النظام يطلب تغييرها أول دخول |
   | `DEMO_PASSWORD` | كلمة مرور لحسابات العرض (`demo-acc` المحاسب و`demo-rev2` المراجع) |

4. اضغط **Apply**. أول نشر ياخذ ٣ إلى ٥ دقائق.
5. الرابط يطلع فوق، مثل `https://miftah-alkhazina.onrender.com`.

## ٤. المشاركة مع المدير

- **الرابط:** رابط Render.
- **للعرض السريع:** ادخل بحساب `demo-acc` (المحاسب) وكلمة `DEMO_PASSWORD`. فيه شركة تجريبية بسياسة معتمدة ومعاملات.
- **لو يبي يجرب الاعتماد:** يدخل بحساب `demo-rev2` (المراجع).
- **لو تبي له حساب خاص:** ادخل بحساب المدير، ثم **المستخدمين**، ثم أضف محاسب.

## ٥. خلّ الموقع يفتح على طول (بدون شاشة «Waking up»)

الخطة المجانية في Render **تنام بعد ١٥ دقيقة** بدون زيارات، وأول فتح بعدها ياخذ تقريبًا دقيقة.
الحل: خدمة مراقبة مجانية تزور الموقع كل ٥ دقائق. نفس الزيارة تلمس قاعدة البيانات،
فتمنع كمان مشروع Supabase المجاني من التوقف بعد أسبوع.

1. سجّل في https://uptimerobot.com (مجاني).
2. اضغط **New monitor** وعبّه كذا:
   - **Monitor type:** HTTP(s)
   - **URL:** `https://miftah-alkhazina.onrender.com/keepalive`
   - **Monitoring interval:** 5 minutes
3. اضغط **Create monitor**.

الساعات المجانية في Render (٧٥٠ ساعة بالشهر) تكفي خدمة وحدة شغّالة طول الشهر.
ولو احتجت ضمان أعلى لاحقًا، الخطة المدفوعة في Render (Starter) ما تنام أصلًا.

## ملاحظات

- لو Supabase توقف رغم كذا، ترجّعه من لوحة Supabase بزر **Restore**.
- أي تحديث للكود يُرفع لـGitHub، وRender ينشره تلقائيًا.
- ما تبي الشركة التجريبية؟ غيّر `DEMO_SEED` إلى `0` في إعدادات Render.
