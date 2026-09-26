"""Signing keys for tests: long enough for the settings, distinct, and plainly not secret.

They repeat a word rather than look random, so that no secret scanner mistakes
them for real keys, and they hold no ``change-me`` marker, so that settings in
production mode accept them where a test needs production settings.
"""

from typing import Final

from pydantic import SecretStr

TEST_JWT_SECRET: Final = SecretStr("jwt-" * 8)
TEST_ORDER_TOKEN_KEY: Final = SecretStr("order-token-" * 3)
# Signs the simulator's comment webhooks in the tests (livecart_core.signing).
TEST_COMMENT_WEBHOOK_SECRET: Final = SecretStr("comment-webhook-" * 2)
# Signs the mock payment provider's webhooks and names its sessions in the tests.
TEST_MOCK_PAYMENT_WEBHOOK_SECRET: Final = SecretStr("mock-payment-" * 3)
# The key and the webhook signing secret of the Stripe adapter in the tests,
# of Stripe's test mode shapes, built from their prefix and a repeated word.
# They sign nothing Stripe would take: no test ever reaches api.stripe.com.
TEST_STRIPE_SECRET_KEY: Final = SecretStr("sk_test_" + "unit" * 6)
TEST_STRIPE_WEBHOOK_SECRET: Final = SecretStr("whsec_" + "unit" * 8)
