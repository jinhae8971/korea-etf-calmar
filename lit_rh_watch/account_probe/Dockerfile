FROM python:3.11-slim
WORKDIR /app
# 토큰 자동 생성용. LIGHTER_AUTH_TOKEN을 직접 주면 SDK 없이도 동작한다.
RUN pip install --no-cache-dir "lighter-sdk>=1.1.4"
COPY probe.py /app/probe.py
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
CMD ["python", "/app/probe.py"]
