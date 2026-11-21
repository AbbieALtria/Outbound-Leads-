"""phone_validation is on for a NEW install only: a database that already holds
leads but never saved the setting stays off, and a saved choice is never
changed."""
import db


def _init(tmp_path, name="leads.db"):
    path = tmp_path / name
    db.init_db(path)
    return path


def test_new_install_defaults_phone_validation_on(tmp_path):
    conn = db.connect(_init(tmp_path))
    assert db.get_setting(conn, "phone_validation") == "1"
    conn.close()


def test_existing_database_without_the_setting_stays_off(tmp_path):
    path = _init(tmp_path)
    conn = db.connect(path)
    # Make it look like a database from before the setting existed: leads
    # present, no saved value.
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date) "
                 "VALUES ('(813) 555-0100', 'B', '2026-07-01')")
    conn.execute("DELETE FROM settings WHERE key = 'phone_validation'")
    conn.commit()
    conn.close()

    db.init_db(path)
    conn = db.connect(path)
    assert db.get_setting(conn, "phone_validation") == "0"
    conn.close()


def test_saved_choice_is_never_changed(tmp_path):
    path = _init(tmp_path)
    for saved in ("0", "1"):
        conn = db.connect(path)
        db.set_setting(conn, "phone_validation", saved)
        conn.commit()
        conn.close()
        db.init_db(path)
        conn = db.connect(path)
        assert db.get_setting(conn, "phone_validation") == saved
        conn.close()
