FROM haproxy:3.4
COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg
# El secreto del JWT llega como Docker secret; HAProxy lo lee como variable de entorno
CMD ["sh", "-c", "export JWT_SECRET=\"$(cat /run/secrets/jwt_secret)\" && exec haproxy -W -db -f /usr/local/etc/haproxy/haproxy.cfg"]
