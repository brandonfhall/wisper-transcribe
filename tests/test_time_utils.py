"""Tests for wisper_transcribe.time_utils."""
from wisper_transcribe.time_utils import format_duration, format_timestamp


class TestFormatTimestamp:
    def test_seconds_only(self):
        assert format_timestamp(45.0) == "00:45"

    def test_minutes_and_seconds(self):
        assert format_timestamp(90.0) == "01:30"

    def test_hours(self):
        assert format_timestamp(3661.0) == "01:01:01"

    def test_zero(self):
        assert format_timestamp(0.0) == "00:00"

    def test_fractional_truncated(self):
        assert format_timestamp(61.9) == "01:01"


class TestFormatDuration:
    def test_zero(self):
        assert format_duration(0) == "0:00:00"

    def test_one_minute_one_second(self):
        assert format_duration(61) == "0:01:01"

    def test_one_hour(self):
        assert format_duration(3600) == "1:00:00"

    def test_complex_duration(self):
        assert format_duration(3725) == "1:02:05"
