ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache     python3     py3-pip     sqlite     tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

RUN chmod a+x /app/run.sh

CMD [ "/app/run.sh" ]
