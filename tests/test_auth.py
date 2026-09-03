import pytest

from exchange.auth import AccountExistsError, AuthStore


def make_store():
    return AuthStore(admin_password="admin-pw", website_password="site-pw")


def test_issue_key_is_idempotent_per_account():
    store = make_store()
    r1 = store.issue_key("student1")
    r2 = store.issue_key("student1")
    assert r1.key == r2.key
    assert store.key_for_account("student1").key == r1.key


def test_regenerate_key_revokes_old_and_issues_new():
    # Regenerating preserves active/password state (a student rotating
    # their own key shouldn't be logged out or need re-activation) — only
    # the key value itself changes.
    store = make_store()
    old = store.issue_key("student1")
    store.activate(old.key)
    new = store.regenerate_key("student1")

    assert new.key != old.key
    assert new.active is True
    assert store.resolve(old.key) is None
    assert store.key_for_account("student1").key == new.key


def test_different_accounts_get_different_keys():
    store = make_store()
    a = store.issue_key("student1")
    b = store.issue_key("student2")
    assert a.key != b.key


def test_list_keys_one_per_account():
    store = make_store()
    store.issue_key("student1")
    store.issue_key("student1")  # idempotent, should not duplicate
    store.issue_key("student2")
    assert len(store.list_keys()) == 2


def test_register_is_active_immediately():
    store = make_store()
    record = store.register("student1", "pw")
    assert record.active is True
    assert store.resolve(record.key) is not None


def test_register_twice_raises_use_login():
    store = make_store()
    store.register("student1", "pw")
    with pytest.raises(AccountExistsError):
        store.register("student1", "pw")


def test_login_verifies_password():
    store = make_store()
    record = store.register("student1", "correct-horse")
    assert store.login("student1", "correct-horse").key == record.key
    assert store.login("student1", "wrong") is None
    assert store.login("nosuchaccount", "pw") is None


def test_register_claims_admin_issued_account():
    store = make_store()
    admin_record = store.issue_key("student1")  # admin path: inactive, no password
    assert admin_record.active is False

    claimed = store.register("student1", "pw")
    assert claimed.key == admin_record.key  # same underlying account/key
    assert claimed.active is True
    assert store.login("student1", "pw").key == admin_record.key
