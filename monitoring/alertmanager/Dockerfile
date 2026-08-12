FROM prom/alertmanager:v0.28.1

COPY config.template.yml /etc/alertmanager/config.template.yml
COPY --chmod=755 render-config.sh /usr/local/bin/render-alertmanager-config

ENTRYPOINT ["/usr/local/bin/render-alertmanager-config"]
CMD ["--config.file=/tmp/alertmanager.yml", "--storage.path=/alertmanager", "--cluster.listen-address="]
