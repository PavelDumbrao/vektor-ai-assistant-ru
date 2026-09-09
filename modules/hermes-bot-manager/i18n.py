from __future__ import annotations

SUPPORTED = ("ru", "en", "es", "de", "fr", "pt", "zh", "ar", "hi", "tr")
DEFAULT_LOCALE = "en"
LEGACY_FALLBACK_LOCALE = "ru"

LANGUAGE_LABELS = {
    "ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "🇪🇸 Español",
    "de": "🇩🇪 Deutsch", "fr": "🇫🇷 Français", "pt": "🇵🇹 Português",
    "zh": "🇨🇳 中文", "ar": "🇸🇦 العربية", "hi": "🇮🇳 हिन्दी", "tr": "🇹🇷 Türkçe",
}

ALIASES = {
    "ru": "ru", "en": "en", "es": "es", "de": "de", "fr": "fr",
    "pt": "pt", "pt-br": "pt", "pt-pt": "pt", "zh": "zh",
    "zh-cn": "zh", "zh-hans": "zh", "zh-tw": "zh", "zh-hant": "zh",
    "ar": "ar", "hi": "hi", "tr": "tr",
}

EN = {
    "menu_hire": "⚡ Hire an AI assistant",
    "menu_my": "🤖 My AI assistants",
    "menu_how": "🧠 How it works",
    "menu_security": "🔐 Security",
    "menu_help": "🛟 Help",
    "menu_language": "🌐 Language",
}

EN.update({
    "welcome": (
        "<b>Hermes Forge | Pro AI</b>\n\nHire your personal AI assistant right in Telegram.\n\n"
        "<b>Self-learning:</b> the more you work with it and correct it, the better it adapts to you.\n\n"
        "<b>Always evolving:</b> as the Hermes ecosystem grows, it receives new shared tools and skills. "
        "Your private data is never mixed with anyone else's.\n\nTap “Hire an AI assistant” and I'll guide you step by step here."
    ),
    "hire_name": (
        "<b>Step 1 of 2. What should your AI assistant be called?</b>\n\n"
        "The display name can be anything. For example:\n<code>Alex AI</code>\n<code>Marcus</code>\n"
        "<code>My Assistant</code>\n\nJust type the name here 👇"
    ),
    "hire_username": (
        "<b>Step 2 of 2. Now choose a username.</b>\n\nThis will be your bot's Telegram address.\n\n"
        "IMPORTANT: the username <b>must end with bot</b>.\nOnly lowercase Latin letters <code>a-z</code>, digits and <code>_</code>. "
        "Length: 5–32 characters.\nIf you use uppercase letters, I'll convert them to lowercase automatically.\n\n"
        "Examples:\n<code>alexai_bot</code>\n<code>markushelperbot</code>\n\nType the username here, with or without @ 👇"
    ),
    "hire_confirm": (
        "<b>Everything is ready. Please check:</b>\n\nName: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n"
        "What happens next:\n1. Tap “Confirm hire”.\n2. Telegram will show one final system confirmation; you won't need to type anything again.\n"
        "3. The bot is created <b>in your Telegram account</b>, just like with BotFather, and belongs to you.\n"
        "4. Hermes Forge will connect it to your AI assistant automatically.\n\n"
        "If Telegram says the username is taken, return here and send a new username as a normal message."
    ),
})

EN.update({
    "confirm_hire": "✅ Confirm hire", "edit_name": "✏️ Change name",
    "edit_username": "✏️ Change username", "cancel": "❌ Cancel",
    "service_unavailable": "⚙️ <b>AI assistant hiring is temporarily unavailable.</b>\n\nPlease try again a little later.",
    "name_empty": "The name cannot be empty. Type any name for your assistant 👇",
    "name_too_long": "The name is too long: {length} characters. Maximum is 64. Shorten it and send again 👇",
    "username_suffix": "username must end with <code>bot</code>",
    "username_length": "username must be 5–32 characters long",
    "username_chars": "use only Latin letters, digits and <code>_</code>",
    "username_fix": "<b>Please fix the username:</b>\n\n• {details}\n\nCorrect example: <code>alexai_bot</code>\n\nSend the corrected username here 👇",
    "username_change": "To change the username, send a new one. Fix:\n\n• {details}\n\nExample: <code>alexai_bot</code>",
    "hire_cancelled": "Hiring cancelled.",
    "hire_cancelled_menu": "Okay, hiring cancelled. When you're ready, tap “Hire an AI assistant”.",
    "hire_prompt": "Tap “⚡ Hire an AI assistant” and I'll guide you step by step.",
    "confirm_wait": "Your details are ready. Tap “✅ Confirm hire” under the previous message or choose what to edit.",
    "created_finishing": "⚙️ Bot created. Finishing the Hermes setup…",
    "my_title": "<b>My AI assistants</b>",
    "my_empty": "<b>My AI assistants</b>\n\nYou haven't hired anyone yet. Tap “Hire an AI assistant” in the main menu.",
    "row_active": "working", "row_inactive": "stopped", "row_imported": "connected earlier",
    "status_summary": "<b>Your AI assistants</b>\n\nConnected: <b>{count}</b>",
    "access_denied": "🔒 <b>Access denied.</b>\n\nHermes Forge is available by invitation only.",
    "language_title": "<b>Choose your language</b>\n\nYou can change it any time.",
    "language_set": "✅ Language changed to {language}.",
})

EN.update({
    "help": "<b>Help</b>\n\n/hire — hire an AI assistant\n/my — my assistants\n/status — status\n/cancel — cancel current hiring\n/menu — main menu\n/language — change language",
    "how": (
        "<b>How it works</b>\n\n1. You give the assistant a name and username right in this chat.\n"
        "2. Telegram asks for one final confirmation.\n3. The bot is created in your account, like with BotFather, and belongs to you.\n"
        "4. We connect Hermes automatically.\n5. The assistant remembers your context and learns from your corrections.\n"
        "6. As the Hermes ecosystem evolves, it receives new shared tools and skills. Your private data stays isolated."
    ),
    "security": (
        "<b>Security</b>\n\n• the bot belongs to you\n• access is restricted by default\n"
        "• bot tokens are never shown in the Forge interface\n• your private data is isolated from other users\n"
        "• the bot token can be rotated without recreating the assistant"
    ),
    "provision_active": "✅ <b>AI assistant is ready.</b>\n\n@{username} is fully deployed and passed health checks. You can start working with it.",
    "provision_failed": "⚠️ <b>@{username} was created, but Hermes setup did not finish.</b>\n\nThe technical error is recorded. You don't need to recreate the bot.",
    "managed_active": "✅ <b>AI assistant hired and ready.</b>\n\n@{username} is connected and running. It will learn your work context and corrections. Access is closed to outsiders.",
    "managed_provisioning": "✅ <b>@{username} was created.</b>\n\nHermes Forge is now deploying the personal environment: memory, runtime, database and tools. I'll notify you when it is ready.",
    "managed_failed": "⚠️ <b>@{username} was created, but Hermes is not deployed yet.</b>\n\nThe error was recorded safely. You do not need to recreate the bot.",
})

RU = {**EN,
    "menu_hire": "⚡ Нанять AI-ассистента", "menu_my": "🤖 Мои AI-ассистенты",
    "menu_how": "🧠 Как это работает", "menu_security": "🔐 Безопасность",
    "menu_help": "🛟 Помощь", "menu_language": "🌐 Язык",
    "welcome": (
        "<b>Hermes Forge | Pro AI</b>\n\nНайми своего персонального AI-ассистента прямо в Telegram.\n\n"
        "<b>Самообучающийся:</b> чем больше ты с ним работаешь и поправляешь его, тем точнее он подстраивается под тебя.\n\n"
        "<b>Постоянно развивается:</b> вместе с экосистемой Hermes он получает новые общие инструменты и навыки. "
        "Твои приватные данные при этом не смешиваются с чужими.\n\n"
        "Нажми «Нанять AI-ассистента», и я проведу тебя по шагам прямо здесь."
    ),
    "hire_name": (
        "<b>Шаг 1 из 2. Как будет называться твой AI-ассистент?</b>\n\nНазвание может быть любым. Например:\n"
        "<code>Салават AI</code>\n<code>Маркус</code>\n<code>Мой ассистент</code>\n\nПросто напиши название сюда 👇"
    ),
    "hire_username": (
        "<b>Шаг 2 из 2. Теперь придумай username.</b>\n\nЭто адрес твоего бота в Telegram.\n\n"
        "ВАЖНО: username <b>обязательно должен заканчиваться на bot</b>.\n"
        "Только строчные латинские буквы <code>a-z</code>, цифры и <code>_</code>. Длина 5–32 символа.\n"
        "Если напишешь заглавные буквы, я сам приведу их к строчным.\n\n"
        "Примеры:\n<code>salavatai_bot</code>\n<code>markushelperbot</code>\n\nНапиши username сюда. Можно с @ или без него 👇"
    ),
}

RU.update({
    "hire_confirm": (
        "<b>Всё готово. Проверь:</b>\n\nИмя: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n"
        "Что произойдёт дальше:\n1. Нажмёшь «Подтвердить найм».\n"
        "2. Telegram покажет одно системное подтверждение создания бота. Ничего заново вводить не нужно.\n"
        "3. Бот создаётся <b>в твоём Telegram-аккаунте</b>, как при создании через BotFather, и принадлежит тебе.\n"
        "4. Hermes Forge автоматически подключит его к твоему AI-ассистенту.\n\n"
        "Если Telegram скажет, что username уже занят, просто вернись сюда и отправь новый username обычным сообщением."
    ),
    "confirm_hire": "✅ Подтвердить найм", "edit_name": "✏️ Изменить имя",
    "edit_username": "✏️ Изменить username", "cancel": "❌ Отмена",
    "service_unavailable": "⚙️ <b>Сервис найма AI-ассистента временно недоступен.</b>\n\nПопробуй немного позже.",
    "name_empty": "Название не может быть пустым. Напиши любое имя для ассистента 👇",
    "name_too_long": "Название слишком длинное: {length} символов. Максимум 64. Сократи название и отправь ещё раз 👇",
    "username_suffix": "username должен обязательно заканчиваться на <code>bot</code>",
    "username_length": "длина username должна быть от 5 до 32 символов",
    "username_chars": "можно использовать только латинские буквы, цифры и знак <code>_</code>",
    "username_fix": "<b>Нужно немного поправить username:</b>\n\n• {details}\n\nПример правильного варианта: <code>salavatai_bot</code>\n\nОтправь исправленный username сюда 👇",
    "username_change": "Если хочешь поменять username, пришли новый вариант. Нужно исправить:\n\n• {details}\n\nПример: <code>salavatai_bot</code>",
    "hire_cancelled": "Найм отменён.", "hire_cancelled_menu": "Ок, найм отменён. Когда будешь готов, нажми «Нанять AI-ассистента».",
    "hire_prompt": "Нажми «⚡ Нанять AI-ассистента», и я проведу тебя по шагам.",
    "confirm_wait": "Данные уже готовы. Нажми «✅ Подтвердить найм» под предыдущим сообщением или выбери, что изменить.",
})

RU.update({
    "created_finishing": "⚙️ Бот создан. Завершаю подключение к Hermes…",
    "my_title": "<b>Мои AI-ассистенты</b>",
    "my_empty": "<b>Мои AI-ассистенты</b>\n\nПока никого не наняли. Нажми «Нанять AI-ассистента» в главном меню.",
    "row_active": "работает", "row_inactive": "остановлен", "row_imported": "подключён ранее",
    "status_summary": "<b>Твои AI-ассистенты</b>\n\nПодключено: <b>{count}</b>",
    "access_denied": "🔒 <b>Доступ закрыт.</b>\n\nHermes Forge работает только по приглашению администратора Pro AI.",
    "language_title": "<b>Выбери язык</b>\n\nЕго можно изменить в любой момент.",
    "language_set": "✅ Язык изменён: {language}.",
    "help": "<b>Помощь</b>\n\n/hire — нанять AI-ассистента\n/my — мои ассистенты\n/status — статус\n/cancel — отменить текущий найм\n/menu — главное меню\n/language — изменить язык",
    "how": (
        "<b>Как это работает</b>\n\n1. Ты даёшь ассистенту имя и username прямо в этом чате.\n"
        "2. Telegram просит одно финальное подтверждение.\n3. Бот создаётся в твоём аккаунте, как через BotFather, и принадлежит тебе.\n"
        "4. Мы автоматически подключаем Hermes.\n5. Ассистент запоминает твой контекст и учится на твоих правках.\n"
        "6. По мере развития экосистемы Hermes он получает новые общие инструменты и навыки. Твои личные данные не смешиваются с чужими."
    ),
    "security": (
        "<b>Безопасность</b>\n\n• созданный бот принадлежит тебе\n• доступ по умолчанию ограничен\n"
        "• токены не показываются в интерфейсе Forge\n• твои приватные данные изолированы от других пользователей\n"
        "• токен можно перевыпустить без пересоздания ассистента"
    ),
})

RU.update({
    "provision_active": "✅ <b>AI-ассистент готов к работе.</b>\n\n@{username} полностью развёрнут и прошёл проверку. Можно открывать его и начинать работать.",
    "provision_failed": "⚠️ <b>@{username} создан, но настройка Hermes не завершилась.</b>\n\nТехническая ошибка уже зафиксирована. Бота заново создавать не нужно.",
    "managed_active": "✅ <b>AI-ассистент нанят и готов к работе.</b>\n\n@{username} уже подключён и запущен. Он будет запоминать твой рабочий контекст и учиться на твоих правках. Доступ для посторонних закрыт.",
    "managed_provisioning": "✅ <b>@{username} создан.</b>\n\nТеперь Hermes Forge автоматически разворачивает персональную среду: память, runtime, базу и инструменты. Когда health-check пройдёт, я напишу сюда, что ассистент готов.",
    "managed_failed": "⚠️ <b>@{username} создан, но Hermes пока не развёрнут.</b>\n\nОшибка зафиксирована безопасно. Бота заново создавать не нужно.",
})

CATALOGS = {"en": EN, "ru": RU}
ES = {**EN,
    "menu_hire": "⚡ Contratar asistente de IA", "menu_my": "🤖 Mis asistentes de IA",
    "menu_how": "🧠 Cómo funciona", "menu_security": "🔐 Seguridad",
    "menu_help": "🛟 Ayuda", "menu_language": "🌐 Idioma",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nContrata tu asistente personal de IA directamente en Telegram.\n\n<b>Autoaprendizaje:</b> cuanto más trabajas con él y lo corriges, mejor se adapta a ti.\n\n<b>Siempre evoluciona:</b> recibe nuevas herramientas y habilidades junto con el ecosistema Hermes. Tus datos privados nunca se mezclan con los de otros.\n\nPulsa «Contratar asistente de IA» y te guiaré paso a paso.",
    "hire_name": "<b>Paso 1 de 2. ¿Cómo se llamará tu asistente de IA?</b>\n\nEl nombre puede ser cualquiera.\n\nEscribe el nombre aquí 👇",
    "hire_username": "<b>Paso 2 de 2. Ahora elige un username.</b>\n\nEs la dirección de tu bot en Telegram.\n\nIMPORTANTE: debe <b>terminar en bot</b>. Solo letras latinas minúsculas <code>a-z</code>, números y <code>_</code>. Longitud: 5–32 caracteres.\nSi usas mayúsculas, las convertiré automáticamente.\n\nEjemplo: <code>alexai_bot</code>\n\nEscríbelo aquí, con o sin @ 👇",
    "hire_confirm": "<b>Todo listo. Comprueba:</b>\n\nNombre: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Pulsa «Confirmar contratación».\n2. Telegram mostrará una única confirmación final.\n3. El bot se crea <b>en tu cuenta de Telegram</b>, como con BotFather, y te pertenece.\n4. Hermes Forge lo conectará automáticamente.\n\nSi el username está ocupado, vuelve aquí y envía otro.",
    "confirm_hire": "✅ Confirmar contratación", "edit_name": "✏️ Cambiar nombre",
    "edit_username": "✏️ Cambiar username", "cancel": "❌ Cancelar",
    "language_title": "<b>Elige tu idioma</b>\n\nPuedes cambiarlo en cualquier momento.",
    "language_set": "✅ Idioma cambiado a {language}.",
}

DE = {**EN,
    "menu_hire": "⚡ KI-Assistenten einstellen", "menu_my": "🤖 Meine KI-Assistenten",
    "menu_how": "🧠 So funktioniert es", "menu_security": "🔐 Sicherheit",
    "menu_help": "🛟 Hilfe", "menu_language": "🌐 Sprache",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nStelle deinen persönlichen KI-Assistenten direkt in Telegram ein.\n\n<b>Selbstlernend:</b> Je mehr du mit ihm arbeitest und ihn korrigierst, desto besser passt er sich an dich an.\n\n<b>Entwickelt sich ständig:</b> Mit dem Hermes-Ökosystem erhält er neue gemeinsame Werkzeuge und Fähigkeiten. Deine privaten Daten bleiben getrennt.\n\nTippe auf „KI-Assistenten einstellen“, ich führe dich Schritt für Schritt durch.",
    "hire_name": "<b>Schritt 1 von 2. Wie soll dein KI-Assistent heißen?</b>\n\nDer Anzeigename kann frei gewählt werden.\n\nSchreibe den Namen hier 👇",
    "hire_username": "<b>Schritt 2 von 2. Wähle jetzt einen Username.</b>\n\nDas ist die Telegram-Adresse deines Bots.\n\nWICHTIG: Der Username muss <b>mit bot enden</b>. Nur kleine lateinische Buchstaben <code>a-z</code>, Ziffern und <code>_</code>, 5–32 Zeichen. Großbuchstaben werden automatisch umgewandelt.\n\nBeispiel: <code>alexai_bot</code>\n\nSchreibe ihn hier, mit oder ohne @ 👇",
    "hire_confirm": "<b>Alles bereit. Prüfe bitte:</b>\n\nName: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Tippe auf „Einstellung bestätigen“.\n2. Telegram zeigt eine letzte Systembestätigung.\n3. Der Bot wird <b>in deinem Telegram-Konto</b> erstellt, wie bei BotFather, und gehört dir.\n4. Hermes Forge verbindet ihn automatisch.\n\nWenn der Username vergeben ist, komme hierher zurück und sende einen neuen.",
    "confirm_hire": "✅ Einstellung bestätigen", "edit_name": "✏️ Namen ändern",
    "edit_username": "✏️ Username ändern", "cancel": "❌ Abbrechen",
    "language_title": "<b>Sprache wählen</b>\n\nDu kannst sie jederzeit ändern.",
    "language_set": "✅ Sprache geändert: {language}.",
}

FR = {**EN,
    "menu_hire": "⚡ Recruter un assistant IA", "menu_my": "🤖 Mes assistants IA",
    "menu_how": "🧠 Comment ça marche", "menu_security": "🔐 Sécurité",
    "menu_help": "🛟 Aide", "menu_language": "🌐 Langue",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nRecrute ton assistant IA personnel directement dans Telegram.\n\n<b>Auto-apprenant :</b> plus tu travailles avec lui et le corriges, plus il s'adapte à toi.\n\n<b>Évolue en continu :</b> avec l'écosystème Hermes, il reçoit de nouveaux outils et compétences partagés. Tes données privées restent isolées.\n\nAppuie sur « Recruter un assistant IA » et je te guide étape par étape.",
    "hire_name": "<b>Étape 1 sur 2. Comment veux-tu appeler ton assistant IA ?</b>\n\nLe nom affiché peut être libre.\n\nÉcris le nom ici 👇",
    "hire_username": "<b>Étape 2 sur 2. Choisis maintenant un username.</b>\n\nC'est l'adresse Telegram de ton bot.\n\nIMPORTANT : il doit <b>se terminer par bot</b>. Uniquement lettres latines minuscules <code>a-z</code>, chiffres et <code>_</code>, 5 à 32 caractères. Les majuscules seront converties automatiquement.\n\nExemple : <code>alexai_bot</code>\n\nÉcris-le ici, avec ou sans @ 👇",
    "hire_confirm": "<b>Tout est prêt. Vérifie :</b>\n\nNom : <b>{name}</b>\nUsername : <b>@{username}</b>\n\n1. Appuie sur « Confirmer le recrutement ».\n2. Telegram affichera une seule confirmation finale.\n3. Le bot est créé <b>dans ton compte Telegram</b>, comme avec BotFather, et t'appartient.\n4. Hermes Forge le connectera automatiquement.\n\nSi le username est pris, reviens ici et envoie-en un nouveau.",
    "confirm_hire": "✅ Confirmer le recrutement", "edit_name": "✏️ Modifier le nom",
    "edit_username": "✏️ Modifier le username", "cancel": "❌ Annuler",
    "language_title": "<b>Choisis ta langue</b>\n\nTu peux la changer à tout moment.",
    "language_set": "✅ Langue changée : {language}.",
}

PT = {**EN,
    "menu_hire": "⚡ Contratar assistente de IA", "menu_my": "🤖 Meus assistentes de IA",
    "menu_how": "🧠 Como funciona", "menu_security": "🔐 Segurança",
    "menu_help": "🛟 Ajuda", "menu_language": "🌐 Idioma",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nContrate seu assistente pessoal de IA diretamente no Telegram.\n\n<b>Autoaprendizagem:</b> quanto mais você trabalha com ele e o corrige, melhor ele se adapta a você.\n\n<b>Evolução contínua:</b> junto com o ecossistema Hermes, ele recebe novas ferramentas e habilidades compartilhadas. Seus dados privados permanecem isolados.\n\nToque em «Contratar assistente de IA» e eu vou orientar você passo a passo.",
    "hire_name": "<b>Etapa 1 de 2. Como seu assistente de IA vai se chamar?</b>\n\nO nome de exibição pode ser qualquer um.\n\nDigite o nome aqui 👇",
    "hire_username": "<b>Etapa 2 de 2. Agora escolha um username.</b>\n\nEsse será o endereço do bot no Telegram.\n\nIMPORTANTE: o username <b>deve terminar em bot</b>. Use apenas letras latinas minúsculas <code>a-z</code>, números e <code>_</code>, com 5–32 caracteres. Letras maiúsculas serão convertidas automaticamente.\n\nExemplo: <code>alexai_bot</code>\n\nDigite aqui, com ou sem @ 👇",
    "hire_confirm": "<b>Tudo pronto. Confira:</b>\n\nNome: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Toque em «Confirmar contratação».\n2. O Telegram mostrará uma única confirmação final.\n3. O bot é criado <b>na sua conta do Telegram</b>, como no BotFather, e pertence a você.\n4. O Hermes Forge fará a conexão automaticamente.\n\nSe o username estiver ocupado, volte aqui e envie outro.",
    "confirm_hire": "✅ Confirmar contratação", "edit_name": "✏️ Alterar nome",
    "edit_username": "✏️ Alterar username", "cancel": "❌ Cancelar",
    "language_title": "<b>Escolha seu idioma</b>\n\nVocê pode alterá-lo a qualquer momento.",
    "language_set": "✅ Idioma alterado para {language}.",
}

ZH = {**EN,
    "menu_hire": "⚡ 雇用 AI 助手", "menu_my": "🤖 我的 AI 助手",
    "menu_how": "🧠 工作方式", "menu_security": "🔐 安全",
    "menu_help": "🛟 帮助", "menu_language": "🌐 语言",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\n直接在 Telegram 中雇用你的个人 AI 助手。\n\n<b>自我学习：</b>你使用并纠正它越多，它就越能适应你。\n\n<b>持续进化：</b>随着 Hermes 生态发展，它会获得新的共享工具和能力。你的私人数据不会与其他用户混合。\n\n点击“雇用 AI 助手”，我会一步一步带你完成。",
    "hire_name": "<b>第 1/2 步：你的 AI 助手叫什么？</b>\n\n显示名称可以任意填写。\n\n请直接在这里输入名称 👇",
    "hire_username": "<b>第 2/2 步：现在选择 username。</b>\n\n这是机器人在 Telegram 中的地址。\n\n重要：username <b>必须以 bot 结尾</b>。只能使用小写英文字母 <code>a-z</code>、数字和 <code>_</code>，长度 5–32 个字符。大写字母会自动转换为小写。\n\n示例：<code>alexai_bot</code>\n\n请在这里输入，可带 @ 或不带 @ 👇",
    "hire_confirm": "<b>已准备好，请确认：</b>\n\n名称：<b>{name}</b>\nUsername：<b>@{username}</b>\n\n1. 点击“确认雇用”。\n2. Telegram 只会显示一次最终系统确认。\n3. 机器人会创建在<b>你的 Telegram 账户</b>中，就像使用 BotFather 一样，并归你所有。\n4. Hermes Forge 会自动完成连接。\n\n如果 username 已被占用，请返回这里发送新的 username。",
    "confirm_hire": "✅ 确认雇用", "edit_name": "✏️ 修改名称",
    "edit_username": "✏️ 修改 username", "cancel": "❌ 取消",
    "language_title": "<b>选择语言</b>\n\n你可以随时更改。",
    "language_set": "✅ 语言已切换为 {language}。",
}

AR = {**EN,
    "menu_hire": "⚡ وظّف مساعد ذكاء اصطناعي", "menu_my": "🤖 مساعدو الذكاء الاصطناعي لدي",
    "menu_how": "🧠 كيف يعمل", "menu_security": "🔐 الأمان",
    "menu_help": "🛟 المساعدة", "menu_language": "🌐 اللغة",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nوظّف مساعد ذكاء اصطناعي شخصيًا داخل Telegram.\n\n<b>يتعلم ذاتيًا:</b> كلما استخدمته وصححت له، أصبح أكثر دقة في التكيف معك.\n\n<b>يتطور باستمرار:</b> يحصل على أدوات ومهارات مشتركة جديدة مع تطور منظومة Hermes. تبقى بياناتك الخاصة معزولة عن الآخرين.\n\nاضغط «وظّف مساعد ذكاء اصطناعي» وسأرشدك خطوة بخطوة.",
    "hire_name": "<b>الخطوة 1 من 2. ما اسم مساعدك بالذكاء الاصطناعي؟</b>\n\nيمكن أن يكون الاسم المعروض أي شيء.\n\nاكتب الاسم هنا 👇",
    "hire_username": "<b>الخطوة 2 من 2. اختر الآن username.</b>\n\nهذا هو عنوان البوت في Telegram.\n\nمهم: يجب أن <b>ينتهي username بكلمة bot</b>. استخدم فقط الأحرف اللاتينية الصغيرة <code>a-z</code> والأرقام و<code>_</code>، بطول 5–32 حرفًا. سأحوّل الأحرف الكبيرة تلقائيًا.\n\nمثال: <code>alexai_bot</code>\n\nاكتبه هنا مع @ أو بدونها 👇",
    "hire_confirm": "<b>كل شيء جاهز. راجع البيانات:</b>\n\nالاسم: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. اضغط «تأكيد التوظيف».\n2. سيعرض Telegram تأكيدًا نهائيًا واحدًا فقط.\n3. سيتم إنشاء البوت <b>في حساب Telegram الخاص بك</b> مثل BotFather وسيكون ملكك.\n4. سيقوم Hermes Forge بربطه تلقائيًا.\n\nإذا كان username مستخدمًا، عد إلى هنا وأرسل username جديدًا.",
    "confirm_hire": "✅ تأكيد التوظيف", "edit_name": "✏️ تغيير الاسم",
    "edit_username": "✏️ تغيير username", "cancel": "❌ إلغاء",
    "language_title": "<b>اختر لغتك</b>\n\nيمكنك تغييرها في أي وقت.",
    "language_set": "✅ تم تغيير اللغة إلى {language}.",
}

HI = {**EN,
    "menu_hire": "⚡ AI सहायक नियुक्त करें", "menu_my": "🤖 मेरे AI सहायक",
    "menu_how": "🧠 यह कैसे काम करता है", "menu_security": "🔐 सुरक्षा",
    "menu_help": "🛟 सहायता", "menu_language": "🌐 भाषा",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nTelegram में ही अपना निजी AI सहायक नियुक्त करें।\n\n<b>स्वयं सीखने वाला:</b> जितना अधिक आप उसके साथ काम करते और उसे सुधारते हैं, उतना बेहतर वह आपके अनुसार ढलता है।\n\n<b>लगातार विकसित होता है:</b> Hermes ecosystem के साथ उसे नए साझा tools और skills मिलते हैं। आपका निजी data अन्य लोगों के data से अलग रहता है।\n\n«AI सहायक नियुक्त करें» दबाएँ, मैं आपको हर कदम पर मार्गदर्शन दूँगा।",
    "hire_name": "<b>चरण 1/2. आपके AI सहायक का नाम क्या होगा?</b>\n\nDisplay name कुछ भी हो सकता है।\n\nनाम यहाँ लिखें 👇",
    "hire_username": "<b>चरण 2/2. अब username चुनें।</b>\n\nयह Telegram में आपके bot का address होगा।\n\nमहत्वपूर्ण: username <b>bot पर समाप्त होना चाहिए</b>। केवल छोटे Latin letters <code>a-z</code>, digits और <code>_</code>, 5–32 characters। बड़े letters अपने-आप lowercase हो जाएँगे।\n\nउदाहरण: <code>alexai_bot</code>\n\nयहाँ लिखें, @ के साथ या बिना 👇",
    "hire_confirm": "<b>सब तैयार है। जाँच लें:</b>\n\nनाम: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. «नियुक्ति की पुष्टि करें» दबाएँ।\n2. Telegram केवल एक अंतिम system confirmation दिखाएगा।\n3. Bot <b>आपके Telegram account</b> में बनेगा, BotFather की तरह, और आपका रहेगा।\n4. Hermes Forge इसे अपने-आप connect करेगा।\n\nअगर username लिया हुआ है, यहाँ वापस आकर नया username भेजें।",
    "confirm_hire": "✅ नियुक्ति की पुष्टि करें", "edit_name": "✏️ नाम बदलें",
    "edit_username": "✏️ username बदलें", "cancel": "❌ रद्द करें",
    "language_title": "<b>अपनी भाषा चुनें</b>\n\nआप इसे कभी भी बदल सकते हैं।",
    "language_set": "✅ भाषा बदलकर {language} कर दी गई।",
}

TR = {**EN,
    "menu_hire": "⚡ AI asistanı işe al", "menu_my": "🤖 AI asistanlarım",
    "menu_how": "🧠 Nasıl çalışır", "menu_security": "🔐 Güvenlik",
    "menu_help": "🛟 Yardım", "menu_language": "🌐 Dil",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nKişisel AI asistanını doğrudan Telegram içinde işe al.\n\n<b>Kendi kendine öğrenir:</b> onunla ne kadar çok çalışır ve düzeltirsen sana o kadar iyi uyum sağlar.\n\n<b>Sürekli gelişir:</b> Hermes ekosistemi geliştikçe yeni ortak araçlar ve yetenekler kazanır. Özel verilerin diğer kullanıcıların verileriyle karışmaz.\n\n«AI asistanı işe al» düğmesine bas, seni adım adım yönlendireceğim.",
    "hire_name": "<b>Adım 1/2. AI asistanının adı ne olsun?</b>\n\nGörünen ad istediğin herhangi bir şey olabilir.\n\nAdı buraya yaz 👇",
    "hire_username": "<b>Adım 2/2. Şimdi bir username seç.</b>\n\nBu, botunun Telegram adresi olacak.\n\nÖNEMLİ: username <b>bot ile bitmelidir</b>. Yalnızca küçük Latin harfleri <code>a-z</code>, rakamlar ve <code>_</code>, 5–32 karakter. Büyük harfleri otomatik olarak küçülteceğim.\n\nÖrnek: <code>alexai_bot</code>\n\nBuraya @ ile veya @ olmadan yaz 👇",
    "hire_confirm": "<b>Her şey hazır. Kontrol et:</b>\n\nAd: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. «İşe almayı onayla» düğmesine bas.\n2. Telegram yalnızca bir son sistem onayı gösterecek.\n3. Bot <b>senin Telegram hesabında</b>, BotFather'da olduğu gibi oluşturulur ve sana ait olur.\n4. Hermes Forge otomatik olarak bağlar.\n\nUsername alınmışsa buraya dönüp yeni bir username gönder.",
    "confirm_hire": "✅ İşe almayı onayla", "edit_name": "✏️ Adı değiştir",
    "edit_username": "✏️ username değiştir", "cancel": "❌ İptal",
    "language_title": "<b>Dilini seç</b>\n\nİstediğin zaman değiştirebilirsin.",
    "language_set": "✅ Dil {language} olarak değiştirildi.",
}

CATALOGS.update({
    "es": ES, "de": DE, "fr": FR, "pt": PT, "zh": ZH,
    "ar": AR, "hi": HI, "tr": TR,
})


def canonical_locale(code: str | None) -> str:
    raw = (code or "").strip().lower().replace("_", "-")
    if raw in ALIASES:
        return ALIASES[raw]
    head = raw.split("-", 1)[0]
    return ALIASES.get(head, DEFAULT_LOCALE)


def locale_for(user_id: int, state: dict, user: dict | None = None) -> str:
    stored = str(state.get("locales", {}).get(str(int(user_id))) or "")
    if stored in SUPPORTED:
        return stored
    if user is not None:
        return canonical_locale(user.get("language_code"))
    return LEGACY_FALLBACK_LOCALE


def remember_locale(user: dict, state: dict) -> str:
    user_id = int(user.get("id") or 0)
    if not user_id:
        return DEFAULT_LOCALE
    locales = state.setdefault("locales", {})
    key = str(user_id)
    if locales.get(key) not in SUPPORTED:
        raw_language = user.get("language_code")
        locales[key] = canonical_locale(raw_language) if raw_language else LEGACY_FALLBACK_LOCALE
    return locales[key]
def set_locale(state: dict, user_id: int, locale: str) -> str:
    if locale not in SUPPORTED:
        raise ValueError("unsupported_locale")
    state.setdefault("locales", {})[str(int(user_id))] = locale
    return locale


def t(locale: str, key: str, **values) -> str:
    catalog = CATALOGS.get(locale, EN)
    template = catalog.get(key, EN.get(key, key))
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def language_keyboard() -> dict:
    buttons = [
        {"text": LANGUAGE_LABELS[code], "callback_data": f"lang:{code}"}
        for code in SUPPORTED
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def language_name(locale: str) -> str:
    return LANGUAGE_LABELS.get(locale, LANGUAGE_LABELS[DEFAULT_LOCALE])
