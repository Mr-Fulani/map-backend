from apps.core.url_security import is_safe_public_http_url


def test_public_https_url_is_allowed():
    assert is_safe_public_http_url('https://cdn.example.com/image.jpg') is True


def test_local_and_private_urls_are_rejected():
    assert is_safe_public_http_url('http://localhost/image.jpg') is False
    assert is_safe_public_http_url('http://127.0.0.1/image.jpg') is False
    assert is_safe_public_http_url('http://169.254.169.254/latest/meta-data/') is False
    assert is_safe_public_http_url('http://10.0.0.5/image.jpg') is False
