FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegrambot.py content.py xfeed.py kai.py price.py ./
COPY content ./content

RUN adduser -D -u 10001 bot
USER bot

ENTRYPOINT [ "python3", "/app/telegrambot.py" ]
