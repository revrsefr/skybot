from unittest import TestCase

from github import format_event, format_event_lines, ghevent, git


class TestGithubFormat(TestCase):
    def test_only_git_is_command(self):
        # All user-facing commands should be under `.git ...`.
        assert hasattr(git, "_hook")
        assert not hasattr(ghevent, "_hook")

    def test_push_event(self):
        event = {
            "type": "PushEvent",
            "actor": {"login": "alice"},
            "repo": {"name": "octo-org/octo-repo"},
            "payload": {
                "ref": "refs/heads/main",
                "size": 2,
                "before": "1111111111111111111111111111111111111111",
                "head": "abcdef0123456789012345678901234567890123",
            },
        }
        out = format_event(event)
        assert out.startswith("[octo-repo] alice pushed 2 commit(s)")
        assert "to main" in out
        assert "https://github.com/octo-org/octo-repo/compare/" in out

    def test_pull_request_event(self):
        event = {
            "type": "PullRequestEvent",
            "actor": {"login": "bob"},
            "repo": {"name": "octo-org/octo-repo"},
            "payload": {
                "action": "opened",
                "pull_request": {
                    "number": 12,
                    "title": "Fix CI",
                    "html_url": "https://github.com/octo-org/octo-repo/pull/12",
                },
            },
        }
        out = format_event(event)
        assert out.startswith("[octo-repo] bob opened PR #12")
        assert "\"Fix CI\"" in out
        assert "https://github.com/octo-org/octo-repo/pull/12" in out

    def test_push_event_lines_include_commit_subjects(self):
        event = {
            "type": "PushEvent",
            "actor": {"login": "alice"},
            "repo": {"name": "octo-org/octo-repo"},
            "payload": {
                "ref": "refs/heads/main",
                "size": 4,
                "commits": [
                    {"sha": "1111111111111111111111111111111111111111", "message": "one"},
                    {"sha": "2222222222222222222222222222222222222222", "message": "two\n\nbody"},
                    {"sha": "3333333333333333333333333333333333333333", "message": "three"},
                    {"sha": "4444444444444444444444444444444444444444", "message": "four"},
                ],
                "before": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        }

        lines = format_event_lines(event)
        assert lines[0].startswith("[octo-repo] alice pushed 4 commit(s)")
        # Only the last 3 commits are shown.
        assert any("2222222" in line and "two" in line for line in lines)
        assert any("3333333" in line and "three" in line for line in lines)
        assert any("4444444" in line and "four" in line for line in lines)
        assert any("(+1 more commits)" in line for line in lines)

    def test_fallback(self):
        event = {"type": "UnknownEvent", "actor": {"login": "carol"}, "repo": {"name": "x/y"}}
        out = format_event(event)
        assert "[y] carol did UnknownEvent in x/y" == out
