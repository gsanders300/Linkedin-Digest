import unittest

from ai_summarizer import _is_retryable


class ApiError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.code = code


class GeminiRetryTests(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        self.assertTrue(_is_retryable(ApiError(429)))
        self.assertTrue(_is_retryable(ApiError(503)))

    def test_authentication_and_bad_request_errors_are_not_retryable(self) -> None:
        self.assertFalse(_is_retryable(ApiError(400)))
        self.assertFalse(_is_retryable(ApiError(401)))
        self.assertFalse(_is_retryable(ApiError(403)))


if __name__ == "__main__":
    unittest.main()
