CREATE TABLE leads (
                psid TEXT PRIMARY KEY,
                name TEXT,
                phone TEXT,
                year TEXT,
                make TEXT,
                model TEXT,
                style TEXT,
                glass TEXT DEFAULT 'Windshield',
                zip TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                slot_date TEXT,
                slot_time TEXT,
                work_order TEXT,
                portal TEXT,
                stage TEXT DEFAULT 'nuevo',
                history TEXT DEFAULT '[]',
                updated_at TEXT
            , admin_note TEXT, slot_id TEXT, slots_json TEXT, pool_phone TEXT, pool_email TEXT, client_phone TEXT, service_mode TEXT, slots_fetched_at TEXT, address_verified TEXT, address_formatted TEXT, lat TEXT, lng TEXT, bot_muted TEXT, slots_error TEXT, finished_at TEXT, booked_at TEXT, confirm_pending TEXT, quoted_price TEXT, last_inbound_at TEXT, nudge_sent TEXT, nudge_at TEXT);
CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
CREATE TABLE pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                value TEXT NOT NULL UNIQUE,
                status TEXT DEFAULT 'free',
                lead_psid TEXT,
                work_order TEXT,
                assigned_at TEXT,
                freed_at TEXT
            );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE safelite_accesos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                name TEXT DEFAULT '',
                woid TEXT DEFAULT '',
                work_order TEXT DEFAULT '',
                portal TEXT DEFAULT '',
                login_url TEXT DEFAULT '',
                click_url TEXT DEFAULT '',
                source TEXT DEFAULT '',
                updated_at TEXT
            );
CREATE INDEX idx_acceso_email ON safelite_accesos(email);
CREATE INDEX idx_acceso_phone ON safelite_accesos(phone);
