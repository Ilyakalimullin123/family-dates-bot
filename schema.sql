-- ============================================================================
-- Схема базы данных PostgreSQL для Telegram-бота контроля семейных дат
-- и рекомендательной системы подарков (накопление датасета + правила/теги)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Таблица пользователей бота
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(128),
    first_name VARCHAR(128),
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Almaty',
    notify_hour SMALLINT NOT NULL DEFAULT 10,
    notify_minute SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

-- 2. Таблица событий (дни рождения, юбилеи, годовщины)
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,               -- Например: "Юбилей мамы Али"
    event_date DATE NOT NULL,                  -- Дата события (с годом рождения/начала для расчёта возраста/лет)
    has_birth_year BOOLEAN NOT NULL DEFAULT TRUE,
    event_type VARCHAR(50) NOT NULL DEFAULT 'birthday', -- birthday, jubilee, anniversary, other
    recipient_gender VARCHAR(20) NOT NULL DEFAULT 'unknown', -- female, male, couple, unknown
    recipient_relation VARCHAR(64) NOT NULL DEFAULT 'relative', -- mom, dad, partner, friend, relative, child, colleague
    recipient_tags TEXT[] NOT NULL DEFAULT '{}', -- Массив тегов интересов: {'цветы', 'охота', 'дом', ...}
    notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    gift_title VARCHAR(255),                   -- Название выбранного/прикрепленного подарка
    gift_url TEXT,                             -- Ссылка на подарок в магазине/маркетплейсе
    gift_photo_file_id TEXT,                   -- Telegram file_id фотографии подарка (или URL картинки)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_notifications ON events(notifications_enabled) WHERE notifications_enabled = TRUE;

-- 3. Лог отправленных уведомлений (защита от повторной отправки в тот же день/год)
CREATE TABLE IF NOT EXISTS notification_logs (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    target_occurrence_date DATE NOT NULL,      -- Дата наступающего события в текущем/следующем году
    days_before INTEGER NOT NULL,              -- За сколько дней отправлено: 30, 21, 14, 7, 1, 0
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, target_occurrence_date, days_before)
);

-- 4. База подарков и идей (стартовые правила + пополняемые пользователями подарки)
CREATE TABLE IF NOT EXISTS gift_catalog (
    id BIGSERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,            -- Например: 'Цветы и флористика', 'Охота и активный отдых'
    target_gender VARCHAR(20) NOT NULL,        -- 'female', 'male', 'any'
    target_relations TEXT[] NOT NULL DEFAULT '{}', -- {'mom', 'partner', 'friend', ...}
    tags TEXT[] NOT NULL DEFAULT '{}',         -- {'цветы', 'охота', 'рыбалка', 'уют', 'гаджеты', ...}
    min_age SMALLINT DEFAULT 0,
    max_age SMALLINT DEFAULT 120,
    shop_url TEXT,
    image_url TEXT,
    base_score FLOAT NOT NULL DEFAULT 1.0,     -- Базовый вес правила
    times_selected INTEGER NOT NULL DEFAULT 0, -- Сколько раз пользователи выбрали этот подарок
    times_rejected INTEGER NOT NULL DEFAULT 0, -- Сколько раз отклонили
    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    is_approved BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gift_catalog_gender ON gift_catalog(target_gender);
CREATE INDEX IF NOT EXISTS idx_gift_catalog_tags ON gift_catalog USING GIN(tags);

-- 5. Таблица взаимодействий пользователей с подарками (Датасет для обучения ML-модели)
-- Каждое сохранение своего подарка, выбор из рекомендаций или дизлайк пишет строку с признаками (features)
CREATE TABLE IF NOT EXISTS gift_ml_dataset (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id BIGINT REFERENCES events(id) ON DELETE SET NULL,
    gift_id BIGINT REFERENCES gift_catalog(id) ON DELETE SET NULL,
    recipient_gender VARCHAR(20) NOT NULL,     -- Признак X1: пол получателя (female / male / couple)
    recipient_age SMALLINT,                    -- Признак X2: возраст или годовщина (например, 55)
    recipient_relation VARCHAR(64),            -- Признак X3: кем приходится (mom, dad, friend, partner...)
    event_type VARCHAR(50),                    -- Признак X4: тип праздника (birthday, jubilee, anniversary)
    recipient_tags TEXT[] NOT NULL DEFAULT '{}', -- Признак X5: теги интересов получателя
    gift_category VARCHAR(100),                -- Категория подарка
    gift_title VARCHAR(255) NOT NULL,          -- Название подарка
    gift_url TEXT,                             -- Ссылка на подарок
    gift_photo_ref TEXT,                       -- file_id или URL картинки
    interaction_type VARCHAR(32) NOT NULL,     -- 'custom_added', 'recommended_selected', 'recommended_rejected'
    label SMALLINT NOT NULL,                   -- Целевая переменная Y для ML: 1 (положительный выбор), 0 (отказ)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_dataset_features ON gift_ml_dataset(recipient_gender, recipient_relation, label);

-- ============================================================================
-- Стартовое наполнение каталога подарков (базовые правила по гендерным группам)
-- ============================================================================
INSERT INTO gift_catalog (title, category, target_gender, target_relations, tags, min_age, max_age, shop_url, base_score)
VALUES
    -- Женская группа (цветы, уют, украшения, спа, впечатления)
    ('Авторский букет пионовидных роз + клубника в шоколаде', 'Цветы и сладости', 'female', '{mom,partner,friend,relative,colleague}', '{цветы,сладости,романтика}', 16, 90, 'https://flowwow.com/', 2.5),
    ('Сертификат в SPA-комплекс и массаж', 'Красота и уход', 'female', '{mom,partner,friend,relative}', '{спа,красота,отдых,цветы}', 20, 80, 'https://chocolife.me/', 2.2),
    ('Ювелирная подвеска или серьги + букет цветов', 'Украшения и цветы', 'female', '{mom,partner,relative}', '{украшения,цветы,юбилей}', 18, 85, 'https://sokolov.ru/', 2.4),
    ('Умный диффузор, кашемировый плед и флорариум', 'Уют и декор', 'female', '{mom,relative,friend,colleague}', '{уют,дом,цветы}', 25, 90, 'https://kaspi.kz/shop/', 2.0),
    ('Стайлер для волос / бьюти-гаджет + букет гортензий', 'Красота и гаджеты', 'female', '{partner,friend,relative}', '{красота,гаджеты,цветы}', 18, 55, 'https://kaspi.kz/shop/', 2.3),

    -- Мужская группа (охотничий магазин, активный отдых, авто, техника, гриль)
    ('Подарочный сертификат в охотничий и рыболовный магазин «Корган» / «ОхотАктив»', 'Охота и рыбалка', 'male', '{dad,partner,friend,relative,colleague}', '{охота,рыбалка,туризм}', 20, 85, 'https://korgan.kz/', 2.6),
    ('Тактический фонарь, мультитул Leatherman и термокружка Stanley', 'Охота и туризм', 'male', '{dad,partner,friend,relative}', '{охота,туризм,инструменты}', 18, 80, 'https://korgan.kz/', 2.5),
    ('Походный набор для гриля/шашлыка в кожаном кейсе (афганский казан / шампуры)', 'Охота и барбекю', 'male', '{dad,partner,friend,relative}', '{охота,гриль,отдых}', 22, 85, 'https://kaspi.kz/shop/', 2.4),
    ('Бинокль высокой кратности / оптический монокуляр для охоты', 'Охота и оптика', 'male', '{dad,partner,friend,relative}', '{охота,туризм,гаджеты}', 25, 80, 'https://korgan.kz/', 2.3),
    ('Пуско-зарядное устройство (бустер) и набор автоинструментов', 'Авто и техника', 'male', '{dad,partner,friend,relative,colleague}', '{авто,инструменты,гаджеты}', 20, 75, 'https://kaspi.kz/shop/', 2.1),

    -- Универсальные подарки (годовщины, семейные даты)
    ('Парный ужин в панорамном ресторане + фотосессия', 'Впечатления', 'couple', '{partner,mom,dad,relative}', '{годовщина,романтика,семья}', 20, 85, 'https://sxodim.com/', 2.4),
    ('Кофемашина для дома с набором зернового кофе', 'Техника для дома', 'any', '{mom,dad,partner,relative,couple}', '{уют,дом,кулинария,юбилей}', 25, 85, 'https://kaspi.kz/shop/', 2.2)
ON CONFLICT DO NOTHING;