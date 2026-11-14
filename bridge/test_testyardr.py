"""Missing cores skip optional native tests; incompatible supplied cores fail."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_model_switch as switch_tests
import testyardr
import yard_commands as y


class NativeFixtureTests(unittest.TestCase):
    def test_missing_or_incompatible_native_core(self):
        case = switch_tests.NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
        with patch.object(testyardr, 'binary', return_value=None), patch.object(testyardr, 'start') as start:
            with self.assertRaisesRegex(unittest.SkipTest, 'crew_handoff_switch v1'):
                case.setUp()
            start.assert_not_called()
        for response in (b'{"crew_handoff_switch":0}', b'{"crew_handoff_switch":true}', b'PRIVATE'):
            with self.subTest(response=response), tempfile.TemporaryDirectory(prefix='mf') as root:
                case = switch_tests.NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
                env = dict(HOME=root, PATH=root + ':/usr/bin:/bin')
                with patch.object(testyardr, 'binary', return_value='/provided/yardr'), \
                        patch.object(testyardr, 'start', return_value=(object(), Path(root), [], env)), \
                        patch.object(testyardr, 'stop'), \
                        patch.object(y, 'run_yardr', return_value=(0, response, b'PRIVATE', None)) as read, \
                        patch.object(case, 'cli', return_value='') as initialize:
                    try:
                        with self.assertRaisesRegex(AssertionError, 'supplied core requires crew_handoff_switch v1'):
                            case.setUp()
                        initialize.assert_called_once_with('init', '--harness', 'claude')
                        self.assertEqual(read.call_count, 1)
                        self.assertEqual(read.call_args.args[0][-4:], ['crew', 'switch', 'capabilities', '--json'])
                    finally:
                        case.doCleanups()


if __name__ == '__main__':
    unittest.main()
