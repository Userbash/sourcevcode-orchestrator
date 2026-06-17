from core.scripts.ping_all_models import classify_mistral_skip_reason, is_mistral_chat_model, parse_mimo_models


def test_is_mistral_chat_model_skips_known_non_chat_variants():
    assert is_mistral_chat_model('mistral-small-latest') is True
    assert is_mistral_chat_model('mistral-embed-2312') is False
    assert is_mistral_chat_model('mistral-moderation-latest') is False
    assert is_mistral_chat_model('mistral-ocr-latest') is False
    assert is_mistral_chat_model('voxtral-mini-tts-latest') is False
    assert is_mistral_chat_model('voxtral-mini-realtime-2602') is False


def test_classify_mistral_skip_reason_is_specific():
    assert classify_mistral_skip_reason('mistral-embed-2312') == 'embedding_model'
    assert classify_mistral_skip_reason('mistral-moderation-latest') == 'moderation_model'
    assert classify_mistral_skip_reason('mistral-ocr-latest') == 'ocr_model'
    assert classify_mistral_skip_reason('voxtral-mini-tts-latest') == 'tts_model'
    assert classify_mistral_skip_reason('voxtral-mini-transcribe-realtime-2602') == 'transcription_model'


def test_parse_mimo_models_reads_verbose_inventory_blocks():
    raw = """openai/gpt-5.4
{
  "id": "gpt-5.4",
  "providerID": "openai",
  "status": "ONLINE",
  "limit": {
    "context": 128000
  }
}
mimo/mimo-auto
{
  "id": "mimo-auto",
  "providerID": "mimo",
  "status": "ONLINE",
  "limit": {
    "context": 64000
  }
}
"""
    assert parse_mimo_models(raw) == ['openai/gpt-5.4', 'mimo/mimo-auto']
