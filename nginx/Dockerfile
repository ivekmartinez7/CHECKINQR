FROM nginx:alpine

# Generate self-signed SSL certificate
RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/ssl && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/selfsigned.key \
        -out /etc/nginx/ssl/selfsigned.crt \
        -subj "/C=MX/ST=Local/L=Local/O=CheckInQR/CN=localhost" \
        -addext "subjectAltName=IP:0.0.0.0,IP:127.0.0.1,DNS:localhost"

COPY nginx.conf /etc/nginx/conf.d/default.conf
