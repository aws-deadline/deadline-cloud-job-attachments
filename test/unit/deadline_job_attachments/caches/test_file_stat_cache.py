# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import stat
from pathlib import Path
from unittest.mock import patch, MagicMock

from deadline.job_attachments.upload import _FileStatCache


class TestFileStatCache:
    def test_get_stat_caches_result(self, tmp_path):
        """Test that stat results are cached and not called multiple times"""
        cache = _FileStatCache()
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock()

            # First call should invoke stat
            result1 = cache._get_stat(test_file)
            assert mock_stat.call_count == 1

            # Second call should use cache
            result2 = cache._get_stat(test_file)
            assert mock_stat.call_count == 1
            assert result1 is result2

    def test_get_stat_handles_missing_file(self, tmp_path):
        """Test that missing files return None and are cached"""
        cache = _FileStatCache()
        missing_file = tmp_path / "missing.txt"

        result1 = cache._get_stat(missing_file)
        result2 = cache._get_stat(missing_file)

        assert result1 is None
        assert result2 is None

    def test_exists_with_existing_file(self, tmp_path):
        """Test exists() returns True for existing files"""
        cache = _FileStatCache()
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")

        assert cache.exists(test_file) is True

    def test_exists_with_missing_file(self, tmp_path):
        """Test exists() returns False for missing files"""
        cache = _FileStatCache()
        missing_file = tmp_path / "missing.txt"

        assert cache.exists(missing_file) is False

    def test_is_dir_with_directory(self, tmp_path):
        """Test is_dir() returns True for directories"""
        cache = _FileStatCache()
        test_dir = tmp_path / "testdir"
        test_dir.mkdir()

        assert cache.is_dir(test_dir) is True

    def test_is_dir_with_file(self, tmp_path):
        """Test is_dir() returns False for files"""
        cache = _FileStatCache()
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")

        assert cache.is_dir(test_file) is False

    def test_is_dir_with_missing_path(self, tmp_path):
        """Test is_dir() returns False for missing paths"""
        cache = _FileStatCache()
        missing_path = tmp_path / "missing"

        assert cache.is_dir(missing_path) is False

    def test_get_size_with_file(self, tmp_path):
        """Test get_size() returns correct file size"""
        cache = _FileStatCache()
        test_file = tmp_path / "test.txt"
        content = "test content"
        test_file.write_text(content)

        size = cache.get_size(test_file)
        assert size == len(content.encode())

    def test_get_size_with_missing_file(self, tmp_path, caplog):
        """Test get_size() returns 0 for missing files and emits the expected message"""
        cache = _FileStatCache()
        missing_file = tmp_path / "missing.txt"

        assert cache.get_size(missing_file) == 0
        assert "Skipping file in size calculation" in caplog.text

    def test_cache_reuse_across_methods(self, tmp_path):
        """Test that cache is shared across different methods"""
        cache = _FileStatCache()
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_mode=stat.S_IFREG, st_size=4)

            # Call different methods
            cache.exists(test_file)
            cache.is_dir(test_file)
            cache.get_size(test_file)

            # Should only call stat once
            assert mock_stat.call_count == 1


class TestFileStatCacheLongPath:
    """Tests for Windows long-path (MAX_PATH >= 260) handling in _FileStatCache.
    Verifies fix for GitHub issue #51: long-path files silently excluded from upload.
    """

    LONG_PATH = "C:\\" + "a" * 250 + "\\file.txt"  # >= 260 chars total

    def test_get_stat_applies_long_path_prefix(self, tmp_path):
        """Test that _get_stat wraps the path with _get_long_path_compatible_path (issue #51)"""
        cache = _FileStatCache()
        mock_stat_result = MagicMock()

        with patch(
            "deadline.job_attachments.upload._get_long_path_compatible_path"
        ) as mock_compat, patch.object(Path, "stat", return_value=mock_stat_result):
            mock_compat.return_value = Path(self.LONG_PATH)

            result = cache._get_stat(self.LONG_PATH)

            # Verify the helper was called with the original path string
            mock_compat.assert_called_once_with(self.LONG_PATH)
            assert result == mock_stat_result

    def test_get_stat_cache_key_is_original_path(self, tmp_path):
        """Test that lru_cache key remains the original path_str, not the prefixed one"""
        cache = _FileStatCache()
        mock_stat_result = MagicMock()

        with patch(
            "deadline.job_attachments.upload._get_long_path_compatible_path"
        ) as mock_compat, patch.object(Path, "stat", return_value=mock_stat_result):
            mock_compat.return_value = Path("\\\\?\\" + self.LONG_PATH)

            # Call twice - second call should use cache
            cache._get_stat(self.LONG_PATH)
            cache._get_stat(self.LONG_PATH)

            # The helper should only be called once (cached on second call)
            assert mock_compat.call_count == 1

    def test_exists_fallback_applies_long_path_prefix(self, tmp_path):
        """Test that exists() fallback wraps path with _get_long_path_compatible_path"""
        cache = _FileStatCache()

        with patch(
            "deadline.job_attachments.upload._get_long_path_compatible_path"
        ) as mock_compat, patch.object(Path, "stat", side_effect=FileNotFoundError), patch.object(
            Path, "exists", return_value=True
        ):
            mock_compat.return_value = Path("\\\\?\\" + self.LONG_PATH)

            result = cache.exists(Path(self.LONG_PATH))

            # The fallback path.exists() should be called on the wrapped path
            assert result is True
            mock_compat.assert_called()

    def test_is_dir_fallback_applies_long_path_prefix(self, tmp_path):
        """Test that is_dir() fallback wraps path with _get_long_path_compatible_path"""
        cache = _FileStatCache()

        with patch(
            "deadline.job_attachments.upload._get_long_path_compatible_path"
        ) as mock_compat, patch.object(Path, "stat", side_effect=FileNotFoundError), patch.object(
            Path, "is_dir", return_value=False
        ):
            mock_compat.return_value = Path("\\\\?\\" + self.LONG_PATH)

            result = cache.is_dir(Path(self.LONG_PATH))

            assert result is False
            mock_compat.assert_called()
