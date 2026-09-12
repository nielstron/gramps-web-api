"""Per-tree invitation templates, without delivering mail."""

from gramps_webapi.api.emails import email_invitation, invitation_text


def test_default_names_tree_and_keeps_setup_link():
    plain, html = email_invitation(
        "https://example.com/stammbaum", "token", "Our family"
    )
    assert "You are invited to Our family" in plain
    assert "https://example.com/stammbaum/api/users/-/invite/?jwt=token" in plain
    assert "7 days" in plain
    assert "Our family" in html


def test_custom_text_is_escaped_and_link_is_always_present():
    config = {
        "email.invitationSubject": "Welcome to {tree_name}\r\nHello",
        "email.invitationMessage": "Hello <script>evil()</script>\nJoin {tree_name}!",
    }
    subject, message = invitation_text("Family <Bond>", "URL", config)
    assert subject == "Welcome to Family <Bond> Hello"
    plain, html = email_invitation(
        "https://example.com/stammbaum", "token", "Family <Bond>", config
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;Bond&gt;" in html
    assert "jwt=token" in plain and "jwt=token" in html
    assert "7 days" in plain


def test_templates_do_not_leak_between_trees_or_recursively_expand():
    assert (
        invitation_text(
            "{invite_url}", "SECRET", {"email.invitationSubject": "{tree_name}"}
        )[0]
        == "{invite_url}"
    )
    assert (
        invitation_text("Other tree", "URL", {})[0] == "You are invited to Other tree"
    )
    assert (
        invitation_text(
            "Tree", "URL", {"email.invitationMessage": "Link: {invite_url}"}
        )[1]
        == "Link: URL"
    )
