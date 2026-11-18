FROM nginx:1.27-alpine
COPY index.html styles.css app.js /usr/share/nginx/html/
HEALTHCHECK --interval=10s --timeout=3s CMD wget -q -O /dev/null http://localhost/ || exit 1
