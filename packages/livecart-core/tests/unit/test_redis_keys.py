import re

from livecart_core.testing.redis_keys import new_test_key_prefix


def test_each_test_gets_a_new_prefix_in_the_test_namespace() -> None:
    first, second = new_test_key_prefix(), new_test_key_prefix()

    assert re.fullmatch(r"test:[0-9a-f]{32}", first)
    assert first != second
