"""单元测试：过滤条件配置值的解析器（容量 / 时长 / 码率）。

钉死的契约：
- 单位语义遵循 IEC 80000-13（KB=1000、KiB=1024；码率十进制 1K=1000bps）；
- 裸数字按基础单位（字节 / 秒 / bps）；
- 空值 = 0（不限制）；
- 无效值显式报错，绝不静默降级为 0（静默禁用过滤条件最难排查）。
"""

import pytest

from src.core.media.parsing import (
    InvalidBitrateError,
    InvalidDurationError,
    InvalidSizeError,
    parse_bitrate_string,
    parse_duration_string,
    parse_size_string,
)


class TestParseSize:
    def test_empty_values_mean_unlimited(self):
        assert parse_size_string(None) == 0
        assert parse_size_string("") == 0

    def test_numbers_are_bytes(self):
        assert parse_size_string(1024) == 1024
        assert parse_size_string(1.5) == 1

    def test_decimal_units(self):
        assert parse_size_string("1MB") == 10**6
        assert parse_size_string("100mb") == 10**8
        assert parse_size_string("1KB") == 1000
        assert parse_size_string("1K") == 1000
        assert parse_size_string("2GB") == 2 * 10**9

    def test_binary_units(self):
        assert parse_size_string("1MiB") == 2**20
        assert parse_size_string("1GiB") == 2**30

    def test_invalid_value_raises(self):
        with pytest.raises(InvalidSizeError):
            parse_size_string("10MB extra")

    def test_unknown_unit_raises(self):
        with pytest.raises(InvalidSizeError):
            parse_size_string("10XB")

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            parse_size_string(True)


class TestParseDuration:
    def test_empty_values_mean_unlimited(self):
        assert parse_duration_string(None) == 0.0
        assert parse_duration_string("") == 0.0

    def test_numbers_are_seconds(self):
        assert parse_duration_string(90) == 90.0
        assert parse_duration_string(1.5) == 1.5

    def test_unit_strings(self):
        assert parse_duration_string("10s") == 10.0
        assert parse_duration_string("2h") == 7200.0
        assert parse_duration_string("5m") == 300.0
        assert parse_duration_string("1d") == 86400.0

    def test_combined_tokens(self):
        assert parse_duration_string("1h30m") == 5400.0
        assert parse_duration_string("1h 30m") == 5400.0
        assert parse_duration_string("1m30s") == 90.0

    def test_decimal_with_unit(self):
        assert parse_duration_string("1.5h") == 5400.0

    def test_long_unit_words(self):
        assert parse_duration_string("2 hours") == 7200.0
        assert parse_duration_string("3min") == 180.0

    def test_invalid_string_raises(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_string("invalid")

    def test_unknown_unit_raises(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_string("10x")

    def test_garbage_suffix_raises(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_string("10s and more")


class TestParseBitrate:
    def test_empty_values_mean_unlimited(self):
        assert parse_bitrate_string(None) == 0.0
        assert parse_bitrate_string("") == 0.0

    def test_numbers_are_bps(self):
        assert parse_bitrate_string(1000) == 1000.0
        assert parse_bitrate_string(1.5) == 1.5

    def test_decimal_prefixes(self):
        """码率词头为十进制（1K=1000），与容量的 IEC 语义一致。"""
        assert parse_bitrate_string("500K") == 500_000.0
        assert parse_bitrate_string("500Kbps") == 500_000.0
        assert parse_bitrate_string("1Mbps") == 1_000_000.0
        assert parse_bitrate_string("1M") == 1_000_000.0
        assert parse_bitrate_string("2G") == 2e9

    def test_bps_suffix_alone(self):
        assert parse_bitrate_string("1000bps") == 1000.0

    def test_size_style_value(self):
        """v1 的 fallback 场景（"1MB"）由同一解析器自然覆盖。"""
        assert parse_bitrate_string("1MB") == 1_000_000.0

    def test_invalid_string_raises(self):
        with pytest.raises(InvalidBitrateError):
            parse_bitrate_string("fast")

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            parse_bitrate_string(False)
