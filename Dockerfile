# syntax=docker/dockerfile:1
FROM python:3.12-slim
ARG VERSION
RUN pip install --no-cache-dir "poordjaevin[local]==${VERSION}"
# Bake the default zero-shot NLI model so CI runners are deterministic
# and need no Hugging Face access at runtime.
RUN python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; AutoTokenizer.from_pretrained('MoritzLaurer/deberta-v3-base-zeroshot-v2.0'); AutoModelForSequenceClassification.from_pretrained('MoritzLaurer/deberta-v3-base-zeroshot-v2.0')"
LABEL org.opencontainers.image.source="https://github.com/Icaro0310/devin-judge" \
      org.opencontainers.image.description="poordjaevin CLI with baked local NLI model: deterministic yes/no gates for CI"
ENTRYPOINT ["poordjaevin"]
CMD ["--help"]
