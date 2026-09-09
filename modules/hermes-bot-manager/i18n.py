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


# Additional major world languages. Keep business logic locale-independent;
# these catalogs override the core client onboarding while secondary strings
# safely fall back to English.
EXTRA_SUPPORTED = ("it", "ja", "ko", "id", "vi", "pl", "uk", "nl", "fa", "he", "th", "bn", "ur", "ms", "fil")
SUPPORTED = SUPPORTED + EXTRA_SUPPORTED

LANGUAGE_LABELS.update({
    "it": "🇮🇹 Italiano", "ja": "🇯🇵 日本語", "ko": "🇰🇷 한국어",
    "id": "🇮🇩 Bahasa Indonesia", "vi": "🇻🇳 Tiếng Việt", "pl": "🇵🇱 Polski",
    "uk": "🇺🇦 Українська", "nl": "🇳🇱 Nederlands", "fa": "🇮🇷 فارسی",
    "he": "🇮🇱 עברית", "th": "🇹🇭 ไทย", "bn": "🇧🇩 বাংলা",
    "ur": "🇵🇰 اردو", "ms": "🇲🇾 Bahasa Melayu", "fil": "🇵🇭 Filipino",
})

ALIASES.update({
    "it": "it", "ja": "ja", "ko": "ko", "id": "id", "in": "id",
    "vi": "vi", "pl": "pl", "uk": "uk", "nl": "nl", "fa": "fa",
    "he": "he", "iw": "he", "th": "th", "bn": "bn", "ur": "ur",
    "ms": "ms", "fil": "fil", "tl": "fil",
})

IT = {**EN,
    "menu_hire": "⚡ Assumi un assistente IA", "menu_my": "🤖 I miei assistenti IA",
    "menu_how": "🧠 Come funziona", "menu_security": "🔐 Sicurezza",
    "menu_help": "🛟 Aiuto", "menu_language": "🌐 Lingua",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nAssumi il tuo assistente IA personale direttamente su Telegram.\n\n<b>Autoapprendimento:</b> più ci lavori e lo correggi, più si adatta a te.\n\n<b>In continua evoluzione:</b> con l'ecosistema Hermes riceve nuovi strumenti e capacità condivise. I tuoi dati privati restano isolati.\n\nTocca «Assumi un assistente IA» e ti guiderò passo dopo passo.",
    "hire_name": "<b>Passaggio 1 di 2. Come vuoi chiamare il tuo assistente IA?</b>\n\nIl nome visualizzato può essere qualsiasi cosa.\n\nScrivi il nome qui 👇",
    "hire_username": "<b>Passaggio 2 di 2. Ora scegli uno username.</b>\n\nSarà l'indirizzo Telegram del tuo bot.\n\nIMPORTANTE: lo username <b>deve terminare con bot</b>. Usa solo lettere latine minuscole <code>a-z</code>, numeri e <code>_</code>, da 5 a 32 caratteri. Le maiuscole verranno convertite automaticamente.\n\nEsempio: <code>alexai_bot</code>\n\nScrivilo qui, con o senza @ 👇",
    "hire_confirm": "<b>Tutto pronto. Controlla:</b>\n\nNome: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Tocca «Conferma assunzione».\n2. Telegram mostrerà una sola conferma finale.\n3. Il bot viene creato <b>nel tuo account Telegram</b>, come con BotFather, e appartiene a te.\n4. Hermes Forge lo collegherà automaticamente.\n\nSe lo username è occupato, torna qui e inviane uno nuovo.",
    "confirm_hire": "✅ Conferma assunzione", "edit_name": "✏️ Cambia nome",
    "edit_username": "✏️ Cambia username", "cancel": "❌ Annulla",
    "language_title": "<b>Scegli la lingua</b>\n\nPuoi cambiarla in qualsiasi momento.",
    "language_set": "✅ Lingua cambiata in {language}.",
}

JA = {**EN,
    "menu_hire": "⚡ AIアシスタントを採用", "menu_my": "🤖 マイAIアシスタント",
    "menu_how": "🧠 仕組み", "menu_security": "🔐 セキュリティ",
    "menu_help": "🛟 ヘルプ", "menu_language": "🌐 言語",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nTelegram上で自分専用のAIアシスタントを採用できます。\n\n<b>自己学習:</b> 使って修正するほど、あなたに合わせて賢くなります。\n\n<b>継続的に進化:</b> Hermesエコシステムの成長とともに、新しい共通ツールや能力が追加されます。個人データは他のユーザーと混ざりません。\n\n「AIアシスタントを採用」を押すと、ここで順番に案内します。",
    "hire_name": "<b>ステップ1/2。AIアシスタントの名前を決めてください。</b>\n\n表示名は自由です。\n\nここに名前を入力してください 👇",
    "hire_username": "<b>ステップ2/2。次にusernameを決めてください。</b>\n\nTelegramでのボットのアドレスになります。\n\n重要: usernameは<b>必ず bot で終わる必要があります</b>。小文字の英字 <code>a-z</code>、数字、<code>_</code> のみ、5〜32文字です。大文字は自動で小文字に変換します。\n\n例: <code>alexai_bot</code>\n\n@あり・なしどちらでも入力できます 👇",
    "hire_confirm": "<b>準備できました。確認してください:</b>\n\n名前: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. 「採用を確認」を押します。\n2. Telegramで最後のシステム確認が1回だけ表示されます。\n3. BotFatherと同じように、ボットは<b>あなたのTelegramアカウント</b>に作成され、あなたの所有物です。\n4. Hermes Forgeが自動で接続します。\n\nusernameが使用済みなら、ここに戻って別のusernameを送ってください。",
    "confirm_hire": "✅ 採用を確認", "edit_name": "✏️ 名前を変更",
    "edit_username": "✏️ usernameを変更", "cancel": "❌ キャンセル",
    "language_title": "<b>言語を選択</b>\n\nいつでも変更できます。",
    "language_set": "✅ 言語を {language} に変更しました。",
}

KO = {**EN,
    "menu_hire": "⚡ AI 어시스턴트 고용", "menu_my": "🤖 내 AI 어시스턴트",
    "menu_how": "🧠 작동 방식", "menu_security": "🔐 보안",
    "menu_help": "🛟 도움말", "menu_language": "🌐 언어",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nTelegram에서 바로 나만의 AI 어시스턴트를 고용하세요.\n\n<b>자기 학습:</b> 더 많이 사용하고 수정할수록 당신에게 더 잘 맞춰집니다.\n\n<b>지속적인 발전:</b> Hermes 생태계가 성장하면서 새로운 공용 도구와 능력을 받습니다. 개인 데이터는 다른 사용자와 섞이지 않습니다.\n\n«AI 어시스턴트 고용»을 누르면 단계별로 안내합니다.",
    "hire_name": "<b>1/2단계. AI 어시스턴트의 이름을 정해주세요.</b>\n\n표시 이름은 자유롭게 정할 수 있습니다.\n\n여기에 이름을 입력하세요 👇",
    "hire_username": "<b>2/2단계. 이제 username을 정해주세요.</b>\n\nTelegram에서 사용할 봇 주소입니다.\n\n중요: username은 <b>반드시 bot으로 끝나야 합니다</b>. 소문자 영문 <code>a-z</code>, 숫자, <code>_</code>만 사용하며 길이는 5–32자입니다. 대문자는 자동으로 소문자로 변환됩니다.\n\n예: <code>alexai_bot</code>\n\n@ 포함 여부와 관계없이 입력하세요 👇",
    "hire_confirm": "<b>준비되었습니다. 확인하세요:</b>\n\n이름: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. «고용 확인»을 누릅니다.\n2. Telegram에서 마지막 시스템 확인이 한 번 표시됩니다.\n3. BotFather와 마찬가지로 봇은 <b>당신의 Telegram 계정</b>에 생성되며 당신의 소유입니다.\n4. Hermes Forge가 자동으로 연결합니다.\n\nusername이 이미 사용 중이면 여기로 돌아와 새 username을 보내세요.",
    "confirm_hire": "✅ 고용 확인", "edit_name": "✏️ 이름 변경",
    "edit_username": "✏️ username 변경", "cancel": "❌ 취소",
    "language_title": "<b>언어 선택</b>\n\n언제든 변경할 수 있습니다.",
    "language_set": "✅ 언어를 {language}(으)로 변경했습니다.",
}

ID = {**EN,
    "menu_hire": "⚡ Rekrut asisten AI", "menu_my": "🤖 Asisten AI saya",
    "menu_how": "🧠 Cara kerja", "menu_security": "🔐 Keamanan",
    "menu_help": "🛟 Bantuan", "menu_language": "🌐 Bahasa",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nRekrut asisten AI pribadi langsung di Telegram.\n\n<b>Belajar sendiri:</b> semakin sering digunakan dan dikoreksi, semakin baik ia menyesuaikan diri dengan Anda.\n\n<b>Terus berkembang:</b> seiring ekosistem Hermes berkembang, ia mendapat alat dan kemampuan bersama yang baru. Data pribadi Anda tetap terisolasi.\n\nTekan «Rekrut asisten AI» dan saya akan memandu langkah demi langkah.",
    "hire_name": "<b>Langkah 1 dari 2. Apa nama asisten AI Anda?</b>\n\nNama tampilan boleh apa saja.\n\nKetik namanya di sini 👇",
    "hire_username": "<b>Langkah 2 dari 2. Sekarang pilih username.</b>\n\nIni akan menjadi alamat bot Anda di Telegram.\n\nPENTING: username <b>harus diakhiri dengan bot</b>. Hanya huruf Latin kecil <code>a-z</code>, angka dan <code>_</code>, panjang 5–32 karakter. Huruf besar akan saya ubah otomatis.\n\nContoh: <code>alexai_bot</code>\n\nKetik di sini, dengan atau tanpa @ 👇",
    "hire_confirm": "<b>Semuanya siap. Periksa:</b>\n\nNama: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Tekan «Konfirmasi perekrutan».\n2. Telegram akan menampilkan satu konfirmasi sistem terakhir.\n3. Bot dibuat <b>di akun Telegram Anda</b>, seperti melalui BotFather, dan menjadi milik Anda.\n4. Hermes Forge akan menghubungkannya otomatis.\n\nJika username sudah dipakai, kembali ke sini dan kirim username baru.",
    "confirm_hire": "✅ Konfirmasi perekrutan", "edit_name": "✏️ Ubah nama",
    "edit_username": "✏️ Ubah username", "cancel": "❌ Batal",
    "language_title": "<b>Pilih bahasa</b>\n\nAnda dapat mengubahnya kapan saja.",
    "language_set": "✅ Bahasa diubah ke {language}.",
}

VI = {**EN,
    "menu_hire": "⚡ Thuê trợ lý AI", "menu_my": "🤖 Trợ lý AI của tôi",
    "menu_how": "🧠 Cách hoạt động", "menu_security": "🔐 Bảo mật",
    "menu_help": "🛟 Trợ giúp", "menu_language": "🌐 Ngôn ngữ",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nThuê trợ lý AI cá nhân ngay trong Telegram.\n\n<b>Tự học:</b> càng sử dụng và chỉnh sửa, trợ lý càng thích nghi tốt hơn với bạn.\n\n<b>Luôn phát triển:</b> khi hệ sinh thái Hermes phát triển, trợ lý nhận thêm các công cụ và khả năng dùng chung. Dữ liệu riêng tư của bạn luôn được tách biệt.\n\nNhấn «Thuê trợ lý AI» và tôi sẽ hướng dẫn từng bước.",
    "hire_name": "<b>Bước 1/2. Bạn muốn đặt tên trợ lý AI là gì?</b>\n\nTên hiển thị có thể tùy ý.\n\nNhập tên tại đây 👇",
    "hire_username": "<b>Bước 2/2. Bây giờ hãy chọn username.</b>\n\nĐây sẽ là địa chỉ bot trên Telegram.\n\nQUAN TRỌNG: username <b>phải kết thúc bằng bot</b>. Chỉ dùng chữ Latin thường <code>a-z</code>, số và <code>_</code>, dài 5–32 ký tự. Chữ hoa sẽ tự động chuyển thành chữ thường.\n\nVí dụ: <code>alexai_bot</code>\n\nNhập tại đây, có hoặc không có @ 👇",
    "hire_confirm": "<b>Mọi thứ đã sẵn sàng. Hãy kiểm tra:</b>\n\nTên: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Nhấn «Xác nhận thuê».\n2. Telegram sẽ hiển thị một xác nhận hệ thống cuối cùng.\n3. Bot được tạo <b>trong tài khoản Telegram của bạn</b>, giống BotFather, và thuộc sở hữu của bạn.\n4. Hermes Forge sẽ tự động kết nối.\n\nNếu username đã được dùng, quay lại đây và gửi username mới.",
    "confirm_hire": "✅ Xác nhận thuê", "edit_name": "✏️ Đổi tên",
    "edit_username": "✏️ Đổi username", "cancel": "❌ Hủy",
    "language_title": "<b>Chọn ngôn ngữ</b>\n\nBạn có thể thay đổi bất cứ lúc nào.",
    "language_set": "✅ Đã đổi ngôn ngữ sang {language}.",
}

PL = {**EN,
    "menu_hire": "⚡ Zatrudnij asystenta AI", "menu_my": "🤖 Moi asystenci AI",
    "menu_how": "🧠 Jak to działa", "menu_security": "🔐 Bezpieczeństwo",
    "menu_help": "🛟 Pomoc", "menu_language": "🌐 Język",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nZatrudnij osobistego asystenta AI bezpośrednio w Telegramie.\n\n<b>Sam się uczy:</b> im więcej z nim pracujesz i go poprawiasz, tym lepiej dopasowuje się do Ciebie.\n\n<b>Stale się rozwija:</b> wraz z ekosystemem Hermes otrzymuje nowe wspólne narzędzia i możliwości. Twoje prywatne dane pozostają odizolowane.\n\nKliknij «Zatrudnij asystenta AI», a przeprowadzę Cię krok po kroku.",
    "hire_name": "<b>Krok 1 z 2. Jak ma nazywać się Twój asystent AI?</b>\n\nNazwa wyświetlana może być dowolna.\n\nWpisz nazwę tutaj 👇",
    "hire_username": "<b>Krok 2 z 2. Teraz wybierz username.</b>\n\nTo będzie adres Twojego bota w Telegramie.\n\nWAŻNE: username <b>musi kończyć się na bot</b>. Tylko małe litery łacińskie <code>a-z</code>, cyfry i <code>_</code>, 5–32 znaki. Wielkie litery zostaną automatycznie zmienione na małe.\n\nPrzykład: <code>alexai_bot</code>\n\nWpisz tutaj, z @ lub bez 👇",
    "hire_confirm": "<b>Wszystko gotowe. Sprawdź:</b>\n\nNazwa: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Kliknij «Potwierdź zatrudnienie».\n2. Telegram pokaże jedno końcowe potwierdzenie systemowe.\n3. Bot zostanie utworzony <b>na Twoim koncie Telegram</b>, jak w BotFather, i będzie należał do Ciebie.\n4. Hermes Forge połączy go automatycznie.\n\nJeśli username jest zajęty, wróć tutaj i wyślij nowy.",
    "confirm_hire": "✅ Potwierdź zatrudnienie", "edit_name": "✏️ Zmień nazwę",
    "edit_username": "✏️ Zmień username", "cancel": "❌ Anuluj",
    "language_title": "<b>Wybierz język</b>\n\nMożesz go zmienić w dowolnej chwili.",
    "language_set": "✅ Język zmieniono na {language}.",
}

UK = {**EN,
    "menu_hire": "⚡ Найняти AI-асистента", "menu_my": "🤖 Мої AI-асистенти",
    "menu_how": "🧠 Як це працює", "menu_security": "🔐 Безпека",
    "menu_help": "🛟 Допомога", "menu_language": "🌐 Мова",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nНайми персонального AI-асистента прямо в Telegram.\n\n<b>Самонавчання:</b> що більше ти з ним працюєш і виправляєш його, то краще він підлаштовується під тебе.\n\n<b>Постійно розвивається:</b> разом з екосистемою Hermes він отримує нові спільні інструменти та можливості. Твої приватні дані залишаються ізольованими.\n\nНатисни «Найняти AI-асистента», і я проведу тебе крок за кроком.",
    "hire_name": "<b>Крок 1 з 2. Як називатиметься твій AI-асистент?</b>\n\nВідображуване ім'я може бути будь-яким.\n\nНапиши ім'я тут 👇",
    "hire_username": "<b>Крок 2 з 2. Тепер обери username.</b>\n\nЦе буде адреса бота в Telegram.\n\nВАЖЛИВО: username <b>має закінчуватися на bot</b>. Лише малі латинські літери <code>a-z</code>, цифри та <code>_</code>, довжина 5–32 символи. Великі літери автоматично перетворяться на малі.\n\nПриклад: <code>alexai_bot</code>\n\nНапиши тут, з @ або без 👇",
    "hire_confirm": "<b>Усе готово. Перевір:</b>\n\nІм'я: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Натисни «Підтвердити найм».\n2. Telegram покаже одне фінальне системне підтвердження.\n3. Бот створюється <b>у твоєму Telegram-акаунті</b>, як через BotFather, і належить тобі.\n4. Hermes Forge автоматично його підключить.\n\nЯкщо username зайнятий, повернися сюди та надішли новий.",
    "confirm_hire": "✅ Підтвердити найм", "edit_name": "✏️ Змінити ім'я",
    "edit_username": "✏️ Змінити username", "cancel": "❌ Скасувати",
    "language_title": "<b>Обери мову</b>\n\nЇї можна змінити будь-коли.",
    "language_set": "✅ Мову змінено на {language}.",
}

NL = {**EN,
    "menu_hire": "⚡ AI-assistent inhuren", "menu_my": "🤖 Mijn AI-assistenten",
    "menu_how": "🧠 Hoe het werkt", "menu_security": "🔐 Beveiliging",
    "menu_help": "🛟 Hulp", "menu_language": "🌐 Taal",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nHuur je persoonlijke AI-assistent rechtstreeks in Telegram.\n\n<b>Zelflerend:</b> hoe meer je ermee werkt en corrigeert, hoe beter hij zich aan jou aanpast.\n\n<b>Blijft zich ontwikkelen:</b> met het Hermes-ecosysteem krijgt hij nieuwe gedeelde tools en mogelijkheden. Je privégegevens blijven geïsoleerd.\n\nTik op «AI-assistent inhuren» en ik begeleid je stap voor stap.",
    "hire_name": "<b>Stap 1 van 2. Hoe moet je AI-assistent heten?</b>\n\nDe weergavenaam mag alles zijn.\n\nTyp de naam hier 👇",
    "hire_username": "<b>Stap 2 van 2. Kies nu een username.</b>\n\nDit wordt het Telegram-adres van je bot.\n\nBELANGRIJK: de username <b>moet eindigen op bot</b>. Alleen kleine Latijnse letters <code>a-z</code>, cijfers en <code>_</code>, 5–32 tekens. Hoofdletters worden automatisch omgezet.\n\nVoorbeeld: <code>alexai_bot</code>\n\nTyp hem hier, met of zonder @ 👇",
    "hire_confirm": "<b>Alles is klaar. Controleer:</b>\n\nNaam: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Tik op «Inhuur bevestigen».\n2. Telegram toont één laatste systeembevestiging.\n3. De bot wordt <b>in jouw Telegram-account</b> aangemaakt, zoals met BotFather, en is van jou.\n4. Hermes Forge koppelt hem automatisch.\n\nAls de username bezet is, kom terug en stuur een nieuwe.",
    "confirm_hire": "✅ Inhuur bevestigen", "edit_name": "✏️ Naam wijzigen",
    "edit_username": "✏️ Username wijzigen", "cancel": "❌ Annuleren",
    "language_title": "<b>Kies je taal</b>\n\nJe kunt deze altijd wijzigen.",
    "language_set": "✅ Taal gewijzigd naar {language}.",
}

FA = {**EN,
    "menu_hire": "⚡ استخدام دستیار هوش مصنوعی", "menu_my": "🤖 دستیارهای هوش مصنوعی من",
    "menu_how": "🧠 نحوه کار", "menu_security": "🔐 امنیت",
    "menu_help": "🛟 راهنما", "menu_language": "🌐 زبان",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nدستیار هوش مصنوعی شخصی خود را مستقیماً در Telegram استخدام کنید.\n\n<b>خودآموز:</b> هرچه بیشتر با آن کار کنید و اصلاحش کنید، بهتر با شما سازگار می‌شود.\n\n<b>همیشه در حال پیشرفت:</b> با رشد اکوسیستم Hermes، ابزارها و قابلیت‌های مشترک جدید دریافت می‌کند. داده‌های خصوصی شما از دیگران جدا می‌ماند.\n\nروی «استخدام دستیار هوش مصنوعی» بزنید تا مرحله‌به‌مرحله راهنمایی‌تان کنم.",
    "hire_name": "<b>مرحله ۱ از ۲. نام دستیار هوش مصنوعی شما چه باشد؟</b>\n\nنام نمایشی می‌تواند هر چیزی باشد.\n\nنام را اینجا بنویسید 👇",
    "hire_username": "<b>مرحله ۲ از ۲. حالا یک username انتخاب کنید.</b>\n\nاین آدرس ربات شما در Telegram خواهد بود.\n\nمهم: username <b>باید با bot تمام شود</b>. فقط حروف کوچک لاتین <code>a-z</code>، اعداد و <code>_</code>، با طول ۵ تا ۳۲ کاراکتر. حروف بزرگ خودکار کوچک می‌شوند.\n\nمثال: <code>alexai_bot</code>\n\nاینجا با @ یا بدون آن وارد کنید 👇",
    "hire_confirm": "<b>همه‌چیز آماده است. بررسی کنید:</b>\n\nنام: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n۱. روی «تأیید استخدام» بزنید.\n۲. Telegram فقط یک تأیید نهایی سیستمی نشان می‌دهد.\n۳. ربات <b>در حساب Telegram شما</b> مانند BotFather ساخته می‌شود و متعلق به شماست.\n۴. Hermes Forge آن را خودکار متصل می‌کند.\n\nاگر username قبلاً گرفته شده، برگردید و username جدیدی بفرستید.",
    "confirm_hire": "✅ تأیید استخدام", "edit_name": "✏️ تغییر نام",
    "edit_username": "✏️ تغییر username", "cancel": "❌ لغو",
    "language_title": "<b>زبان خود را انتخاب کنید</b>\n\nهر زمان بخواهید می‌توانید آن را تغییر دهید.",
    "language_set": "✅ زبان به {language} تغییر کرد.",
}

HE = {**EN,
    "menu_hire": "⚡ שכירת עוזר AI", "menu_my": "🤖 עוזרי ה-AI שלי",
    "menu_how": "🧠 איך זה עובד", "menu_security": "🔐 אבטחה",
    "menu_help": "🛟 עזרה", "menu_language": "🌐 שפה",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nשכור עוזר AI אישי ישירות ב-Telegram.\n\n<b>לומד בעצמו:</b> ככל שתעבוד איתו ותתקן אותו יותר, הוא יתאים את עצמו אליך טוב יותר.\n\n<b>מתפתח כל הזמן:</b> עם התפתחות אקוסיסטם Hermes הוא מקבל כלים ויכולות משותפים חדשים. הנתונים הפרטיים שלך נשארים מבודדים.\n\nלחץ על «שכירת עוזר AI» ואדריך אותך שלב אחר שלב.",
    "hire_name": "<b>שלב 1 מתוך 2. איך תרצה לקרוא לעוזר ה-AI שלך?</b>\n\nשם התצוגה יכול להיות כל דבר.\n\nכתוב את השם כאן 👇",
    "hire_username": "<b>שלב 2 מתוך 2. עכשיו בחר username.</b>\n\nזו תהיה כתובת הבוט שלך ב-Telegram.\n\nחשוב: ה-username <b>חייב להסתיים ב-bot</b>. רק אותיות לטיניות קטנות <code>a-z</code>, ספרות ו-<code>_</code>, באורך 5–32 תווים. אותיות גדולות יומרו אוטומטית.\n\nדוגמה: <code>alexai_bot</code>\n\nכתוב כאן עם @ או בלעדיו 👇",
    "hire_confirm": "<b>הכול מוכן. בדוק:</b>\n\nשם: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. לחץ על «אישור שכירה».\n2. Telegram יציג אישור מערכת סופי אחד.\n3. הבוט נוצר <b>בחשבון ה-Telegram שלך</b>, כמו דרך BotFather, ושייך לך.\n4. Hermes Forge יחבר אותו אוטומטית.\n\nאם ה-username תפוס, חזור לכאן ושלח חדש.",
    "confirm_hire": "✅ אישור שכירה", "edit_name": "✏️ שינוי שם",
    "edit_username": "✏️ שינוי username", "cancel": "❌ ביטול",
    "language_title": "<b>בחר שפה</b>\n\nאפשר לשנות אותה בכל רגע.",
    "language_set": "✅ השפה שונתה ל-{language}.",
}

TH = {**EN,
    "menu_hire": "⚡ จ้างผู้ช่วย AI", "menu_my": "🤖 ผู้ช่วย AI ของฉัน",
    "menu_how": "🧠 วิธีการทำงาน", "menu_security": "🔐 ความปลอดภัย",
    "menu_help": "🛟 ความช่วยเหลือ", "menu_language": "🌐 ภาษา",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nจ้างผู้ช่วย AI ส่วนตัวได้โดยตรงใน Telegram\n\n<b>เรียนรู้ด้วยตัวเอง:</b> ยิ่งคุณใช้งานและแก้ไขมากเท่าไร ผู้ช่วยก็ยิ่งปรับตัวเข้ากับคุณได้ดีขึ้น\n\n<b>พัฒนาอย่างต่อเนื่อง:</b> เมื่อระบบนิเวศ Hermes เติบโต ผู้ช่วยจะได้รับเครื่องมือและความสามารถส่วนกลางใหม่ ๆ ข้อมูลส่วนตัวของคุณยังคงแยกจากผู้อื่น\n\nกด «จ้างผู้ช่วย AI» แล้วฉันจะพาคุณทำทีละขั้นตอน",
    "hire_name": "<b>ขั้นตอน 1 จาก 2 ตั้งชื่อผู้ช่วย AI ของคุณ</b>\n\nชื่อที่แสดงจะเป็นอะไรก็ได้\n\nพิมพ์ชื่อที่นี่ 👇",
    "hire_username": "<b>ขั้นตอน 2 จาก 2 เลือก username</b>\n\nนี่จะเป็นที่อยู่ของบอตใน Telegram\n\nสำคัญ: username <b>ต้องลงท้ายด้วย bot</b> ใช้ได้เฉพาะอักษรละตินตัวเล็ก <code>a-z</code> ตัวเลข และ <code>_</code> ความยาว 5–32 ตัวอักษร ตัวพิมพ์ใหญ่จะถูกแปลงอัตโนมัติ\n\nตัวอย่าง: <code>alexai_bot</code>\n\nพิมพ์ที่นี่ มี @ หรือไม่มีก็ได้ 👇",
    "hire_confirm": "<b>พร้อมแล้ว โปรดตรวจสอบ:</b>\n\nชื่อ: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. กด «ยืนยันการจ้าง»\n2. Telegram จะแสดงการยืนยันระบบครั้งสุดท้ายเพียงครั้งเดียว\n3. บอตจะถูกสร้าง <b>ในบัญชี Telegram ของคุณ</b> เหมือน BotFather และเป็นของคุณ\n4. Hermes Forge จะเชื่อมต่อให้อัตโนมัติ\n\nหาก username ถูกใช้แล้ว ให้กลับมาที่นี่และส่ง username ใหม่",
    "confirm_hire": "✅ ยืนยันการจ้าง", "edit_name": "✏️ เปลี่ยนชื่อ",
    "edit_username": "✏️ เปลี่ยน username", "cancel": "❌ ยกเลิก",
    "language_title": "<b>เลือกภาษา</b>\n\nเปลี่ยนได้ทุกเมื่อ",
    "language_set": "✅ เปลี่ยนภาษาเป็น {language} แล้ว",
}

BN = {**EN,
    "menu_hire": "⚡ AI সহকারী নিয়োগ করুন", "menu_my": "🤖 আমার AI সহকারীরা",
    "menu_how": "🧠 কীভাবে কাজ করে", "menu_security": "🔐 নিরাপত্তা",
    "menu_help": "🛟 সহায়তা", "menu_language": "🌐 ভাষা",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nTelegram-এর মধ্যেই আপনার ব্যক্তিগত AI সহকারী নিয়োগ করুন।\n\n<b>স্বশিক্ষণ:</b> যত বেশি ব্যবহার ও সংশোধন করবেন, তত ভালোভাবে এটি আপনার সঙ্গে মানিয়ে নেবে।\n\n<b>নিরন্তর উন্নতি:</b> Hermes ecosystem বাড়ার সঙ্গে নতুন শেয়ার করা টুল ও সক্ষমতা পায়। আপনার ব্যক্তিগত ডেটা অন্যদের থেকে আলাদা থাকে।\n\n«AI সহকারী নিয়োগ করুন» চাপুন, আমি ধাপে ধাপে গাইড করব।",
    "hire_name": "<b>ধাপ ১/২। আপনার AI সহকারীর নাম কী হবে?</b>\n\nডিসপ্লে নাম যেকোনো হতে পারে।\n\nএখানে নাম লিখুন 👇",
    "hire_username": "<b>ধাপ ২/২। এখন একটি username বেছে নিন।</b>\n\nএটি Telegram-এ আপনার bot-এর ঠিকানা হবে।\n\nগুরুত্বপূর্ণ: username <b>bot দিয়ে শেষ হতে হবে</b>। শুধু ছোট ল্যাটিন অক্ষর <code>a-z</code>, সংখ্যা এবং <code>_</code>, দৈর্ঘ্য ৫–৩২ অক্ষর। বড় অক্ষর স্বয়ংক্রিয়ভাবে ছোট করা হবে।\n\nউদাহরণ: <code>alexai_bot</code>\n\n@ সহ বা ছাড়া এখানে লিখুন 👇",
    "hire_confirm": "<b>সব প্রস্তুত। যাচাই করুন:</b>\n\nনাম: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n১. «নিয়োগ নিশ্চিত করুন» চাপুন।\n২. Telegram একটি চূড়ান্ত system confirmation দেখাবে।\n৩. BotFather-এর মতো botটি <b>আপনার Telegram account-এ</b> তৈরি হবে এবং আপনারই থাকবে।\n৪. Hermes Forge স্বয়ংক্রিয়ভাবে সংযুক্ত করবে।\n\nusername নেওয়া থাকলে এখানে ফিরে নতুন username পাঠান।",
    "confirm_hire": "✅ নিয়োগ নিশ্চিত করুন", "edit_name": "✏️ নাম পরিবর্তন",
    "edit_username": "✏️ username পরিবর্তন", "cancel": "❌ বাতিল",
    "language_title": "<b>ভাষা বেছে নিন</b>\n\nযেকোনো সময় পরিবর্তন করতে পারেন।",
    "language_set": "✅ ভাষা {language} করা হয়েছে।",
}

UR = {**EN,
    "menu_hire": "⚡ AI اسسٹنٹ کی خدمات حاصل کریں", "menu_my": "🤖 میرے AI اسسٹنٹس",
    "menu_how": "🧠 یہ کیسے کام کرتا ہے", "menu_security": "🔐 سیکیورٹی",
    "menu_help": "🛟 مدد", "menu_language": "🌐 زبان",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nTelegram میں ہی اپنا ذاتی AI اسسٹنٹ حاصل کریں۔\n\n<b>خود سیکھنے والا:</b> جتنا زیادہ آپ اس کے ساتھ کام اور اصلاح کرتے ہیں، اتنا بہتر یہ آپ کے مطابق ڈھلتا ہے۔\n\n<b>مسلسل ترقی:</b> Hermes ecosystem کے ساتھ اسے نئے مشترکہ tools اور capabilities ملتے رہتے ہیں۔ آپ کا نجی data دوسروں سے الگ رہتا ہے۔\n\n«AI اسسٹنٹ کی خدمات حاصل کریں» دبائیں، میں قدم بہ قدم رہنمائی کروں گا۔",
    "hire_name": "<b>مرحلہ 1/2۔ آپ کے AI اسسٹنٹ کا نام کیا ہو؟</b>\n\nDisplay name کچھ بھی ہو سکتا ہے۔\n\nنام یہاں لکھیں 👇",
    "hire_username": "<b>مرحلہ 2/2۔ اب username منتخب کریں۔</b>\n\nیہ Telegram میں آپ کے bot کا address ہوگا۔\n\nاہم: username <b>bot پر ختم ہونا چاہیے</b>۔ صرف چھوٹے Latin letters <code>a-z</code>، digits اور <code>_</code>، لمبائی 5–32 characters۔ بڑے letters خودکار طور پر lowercase ہو جائیں گے۔\n\nمثال: <code>alexai_bot</code>\n\nیہاں @ کے ساتھ یا بغیر لکھیں 👇",
    "hire_confirm": "<b>سب تیار ہے۔ چیک کریں:</b>\n\nنام: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. «ہائر کی تصدیق» دبائیں۔\n2. Telegram صرف ایک آخری system confirmation دکھائے گا۔\n3. BotFather کی طرح bot <b>آپ کے Telegram account</b> میں بنے گا اور آپ کا ہوگا۔\n4. Hermes Forge خودکار طور پر connect کرے گا۔\n\nاگر username پہلے سے لیا گیا ہو تو واپس آ کر نیا username بھیجیں۔",
    "confirm_hire": "✅ ہائر کی تصدیق", "edit_name": "✏️ نام تبدیل کریں",
    "edit_username": "✏️ username تبدیل کریں", "cancel": "❌ منسوخ",
    "language_title": "<b>اپنی زبان منتخب کریں</b>\n\nآپ اسے کسی بھی وقت بدل سکتے ہیں۔",
    "language_set": "✅ زبان {language} کر دی گئی۔",
}

MS = {**EN,
    "menu_hire": "⚡ Ambil pembantu AI", "menu_my": "🤖 Pembantu AI saya",
    "menu_how": "🧠 Cara ia berfungsi", "menu_security": "🔐 Keselamatan",
    "menu_help": "🛟 Bantuan", "menu_language": "🌐 Bahasa",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nAmbil pembantu AI peribadi terus di Telegram.\n\n<b>Belajar sendiri:</b> semakin banyak anda menggunakannya dan membetulkannya, semakin baik ia menyesuaikan diri dengan anda.\n\n<b>Sentiasa berkembang:</b> bersama ekosistem Hermes, ia mendapat alat dan keupayaan bersama yang baharu. Data peribadi anda kekal terasing.\n\nTekan «Ambil pembantu AI» dan saya akan membimbing anda langkah demi langkah.",
    "hire_name": "<b>Langkah 1 daripada 2. Apakah nama pembantu AI anda?</b>\n\nNama paparan boleh apa sahaja.\n\nTaip nama di sini 👇",
    "hire_username": "<b>Langkah 2 daripada 2. Sekarang pilih username.</b>\n\nIni akan menjadi alamat bot anda di Telegram.\n\nPENTING: username <b>mesti berakhir dengan bot</b>. Hanya huruf Latin kecil <code>a-z</code>, nombor dan <code>_</code>, panjang 5–32 aksara. Huruf besar akan ditukar secara automatik.\n\nContoh: <code>alexai_bot</code>\n\nTaip di sini, dengan atau tanpa @ 👇",
    "hire_confirm": "<b>Semuanya sedia. Semak:</b>\n\nNama: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Tekan «Sahkan pengambilan».\n2. Telegram akan menunjukkan satu pengesahan sistem terakhir.\n3. Bot dibuat <b>dalam akaun Telegram anda</b>, seperti melalui BotFather, dan milik anda.\n4. Hermes Forge akan menyambungkannya secara automatik.\n\nJika username telah digunakan, kembali ke sini dan hantar username baharu.",
    "confirm_hire": "✅ Sahkan pengambilan", "edit_name": "✏️ Tukar nama",
    "edit_username": "✏️ Tukar username", "cancel": "❌ Batal",
    "language_title": "<b>Pilih bahasa</b>\n\nAnda boleh menukarnya bila-bila masa.",
    "language_set": "✅ Bahasa ditukar kepada {language}.",
}

FIL = {**EN,
    "menu_hire": "⚡ Kumuha ng AI assistant", "menu_my": "🤖 Mga AI assistant ko",
    "menu_how": "🧠 Paano ito gumagana", "menu_security": "🔐 Seguridad",
    "menu_help": "🛟 Tulong", "menu_language": "🌐 Wika",
    "welcome": "<b>Hermes Forge | Pro AI</b>\n\nKumuha ng personal na AI assistant direkta sa Telegram.\n\n<b>Self-learning:</b> habang mas ginagamit at itinatama mo ito, mas mahusay itong umaangkop sa iyo.\n\n<b>Patuloy na umuunlad:</b> habang lumalaki ang Hermes ecosystem, nakakakuha ito ng mga bagong shared tool at kakayahan. Nananatiling hiwalay ang pribado mong data.\n\nPindutin ang «Kumuha ng AI assistant» at gagabayan kita hakbang-hakbang.",
    "hire_name": "<b>Hakbang 1 sa 2. Ano ang pangalan ng AI assistant mo?</b>\n\nPuwedeng kahit ano ang display name.\n\nI-type ang pangalan dito 👇",
    "hire_username": "<b>Hakbang 2 sa 2. Pumili naman ng username.</b>\n\nIto ang magiging Telegram address ng bot mo.\n\nMAHALAGA: ang username ay <b>dapat magtapos sa bot</b>. Maliit na Latin letters <code>a-z</code>, numero at <code>_</code> lamang, 5–32 characters. Awtomatikong gagawing lowercase ang uppercase.\n\nHalimbawa: <code>alexai_bot</code>\n\nI-type dito, may @ man o wala 👇",
    "hire_confirm": "<b>Handa na. Suriin:</b>\n\nPangalan: <b>{name}</b>\nUsername: <b>@{username}</b>\n\n1. Pindutin ang «Kumpirmahin ang pagkuha».\n2. Magpapakita ang Telegram ng isang huling system confirmation.\n3. Gagawin ang bot <b>sa Telegram account mo</b>, gaya ng BotFather, at pag-aari mo ito.\n4. Awtomatikong ikokonekta ito ng Hermes Forge.\n\nKung gamit na ang username, bumalik dito at magpadala ng bago.",
    "confirm_hire": "✅ Kumpirmahin ang pagkuha", "edit_name": "✏️ Palitan ang pangalan",
    "edit_username": "✏️ Palitan ang username", "cancel": "❌ Kanselahin",
    "language_title": "<b>Pumili ng wika</b>\n\nMaaari mo itong baguhin anumang oras.",
    "language_set": "✅ Binago ang wika sa {language}.",
}

CATALOGS.update({
    "it": IT, "ja": JA, "ko": KO, "id": ID, "vi": VI,
    "pl": PL, "uk": UK, "nl": NL, "fa": FA, "he": HE,
    "th": TH, "bn": BN, "ur": UR, "ms": MS, "fil": FIL,
})


PANEL_LABELS = {
    "ru": "⚙️ Панель Hermes", "en": "⚙️ Hermes Control",
    "es": "⚙️ Panel de Hermes", "de": "⚙️ Hermes-Steuerung",
    "fr": "⚙️ Panneau Hermes", "pt": "⚙️ Painel Hermes",
    "zh": "⚙️ Hermes 控制台", "ar": "⚙️ لوحة Hermes",
    "hi": "⚙️ Hermes पैनल", "tr": "⚙️ Hermes Paneli",
    "it": "⚙️ Pannello Hermes", "ja": "⚙️ Hermes 管理",
    "ko": "⚙️ Hermes 관리", "id": "⚙️ Panel Hermes",
    "vi": "⚙️ Bảng điều khiển Hermes", "pl": "⚙️ Panel Hermes",
    "uk": "⚙️ Панель Hermes", "nl": "⚙️ Hermes-paneel",
    "fa": "⚙️ پنل Hermes", "he": "⚙️ לוח Hermes",
    "th": "⚙️ แผง Hermes", "bn": "⚙️ Hermes প্যানেল",
    "ur": "⚙️ Hermes پینل", "ms": "⚙️ Panel Hermes",
    "fil": "⚙️ Panel ng Hermes",
}
for _locale, _label in PANEL_LABELS.items():
    CATALOGS[_locale]["menu_panel"] = _label


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
