import unittest

from app.main import _parse_tool_call


class ToolCallParsingTests(unittest.TestCase):
    def setUp(self):
        self.tools = [{
            "type": "function",
            "function": {
                "name": "terminal",
                "parameters": {"type": "object"},
            },
        }]

    def test_bare_deepseek_command_maps_to_terminal(self):
        calls, visible_text = _parse_tool_call(
            '{"command":"echo HERMES_TOOL_OK"}', self.tools
        )

        self.assertEqual(
            calls,
            [{"name": "terminal", "arguments": {"command": "echo HERMES_TOOL_OK"}}],
        )
        self.assertEqual(visible_text, "")

    def test_bare_command_is_not_guessed_without_terminal(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "file_read",
                "parameters": {"type": "object"},
            },
        }]

        calls, visible_text = _parse_tool_call('{"command":"echo unsafe"}', tools)

        self.assertEqual(calls, [])
        self.assertEqual(visible_text, '{"command":"echo unsafe"}')

    def test_bash_code_block_maps_to_terminal(self):
        calls, visible_text = _parse_tool_call(
            "```bash\necho HERMES_TOOL_OK\n```", self.tools
        )

        self.assertEqual(
            calls,
            [{"name": "terminal", "arguments": {"command": "echo HERMES_TOOL_OK"}}],
        )
        self.assertEqual(visible_text, "")

    def test_malformed_deepseek_json_maps_first_command_to_terminal(self):
        response = (
            'Ставлю gitleaks.\n'
            '{"command":"go install github.com/gitleaks/gitleaks/v8@latest 2>&1; '
            'echo "EXIT=$?"","timeout":300}\n'
            '{"command":"go env GOPATH"}'
        )

        calls, visible_text = _parse_tool_call(response, self.tools)

        self.assertEqual(
            calls,
            [{
                "name": "terminal",
                "arguments": {
                    "command": (
                        'go install github.com/gitleaks/gitleaks/v8@latest 2>&1; '
                        'echo "EXIT=$?"'
                    ),
                    "timeout": 300,
                },
            }],
        )
        self.assertEqual(
            visible_text,
            'Ставлю gitleaks.\n{"command":"go env GOPATH"}',
        )

