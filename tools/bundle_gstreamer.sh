#!/bin/bash
# Сборка комплекта GStreamer с клиентом PipeWire для вложения в образ.
#
# Использование: tools/bundle_gstreamer.sh КАТАЛОГ
#
# Комплект собирается из пакетов системы сборки: утилиты запуска и
# проверки, модули GStreamer, нужные для записи экрана и снимков без
# указателя, клиентские модули PipeWire, модули SPA и все библиотеки, от
# которых они зависят. Базовые библиотеки, обязательно присутствующие в
# любой системе и привязанные к её ядру (glibc и загрузчик), не копируются.
#
# Приложение пользуется комплектом, только если системный GStreamer
# отсутствует или неполон, см. capture/gstreamer.py.

set -euo pipefail
TARGET="$1"
ARCH_LIB=/usr/lib/x86_64-linux-gnu
GST_PLUGINS="$ARCH_LIB/gstreamer-1.0"
GST_HELPERS="$ARCH_LIB/gstreamer1.0/gstreamer-1.0"
PW_MODULES="$ARCH_LIB/pipewire-0.3"
SPA_PLUGINS="$ARCH_LIB/spa-0.2"

# Модули GStreamer: источник PipeWire, преобразование, вырезание, частота
# кадров, кодировщик PNG и базовые элементы (запись в файл).
PLUGINS=(pipewire videoconvertscale videocrop videorate png coreelements)
# Клиентские модули PipeWire, загружаемые по client.conf.
MODULES=(protocol-native client-node client-device adapter metadata session-manager rt)
# Модули SPA, нужные клиенту: базовая поддержка и преобразователи потоков.
SPA=(support videoconvert audioconvert control)

# Библиотеки, которые не вкладываются: они есть в любой системе и должны
# совпадать с её ядром и загрузчиком.
EXCLUDE='^(linux-vdso|ld-linux|libc\.so|libm\.so|libdl\.so|libpthread\.so|librt\.so|libresolv\.so|libutil\.so)'

for tool in gst-launch-1.0 gst-inspect-1.0; do
    command -v "$tool" >/dev/null || { echo "Нет $tool: требуется gstreamer1.0-tools" >&2; exit 1; }
done

rm -rf "$TARGET"
mkdir -p "$TARGET/bin" "$TARGET/lib" "$TARGET/plugins" \
    "$TARGET/pipewire/modules" "$TARGET/pipewire/spa" "$TARGET/pipewire/config"

cp -L "$(command -v gst-launch-1.0)" "$(command -v gst-inspect-1.0)" "$TARGET/bin/"
cp -L "$GST_HELPERS/gst-plugin-scanner" "$TARGET/bin/"
for plugin in "${PLUGINS[@]}"; do
    cp -L "$GST_PLUGINS/libgst$plugin.so" "$TARGET/plugins/"
done
for module in "${MODULES[@]}"; do
    cp -L "$PW_MODULES/libpipewire-module-$module.so" "$TARGET/pipewire/modules/"
done
for plugin in "${SPA[@]}"; do
    cp -rL "$SPA_PLUGINS/$plugin" "$TARGET/pipewire/spa/"
done
cp -L /usr/share/pipewire/client.conf "$TARGET/pipewire/config/"

# Замыкание зависимостей: библиотеки всех скопированных файлов.
mapfile -t objects < <(find "$TARGET" -type f \( -name '*.so*' -o -perm -u+x \))
for object in "${objects[@]}"; do
    ldd "$object" 2>/dev/null | awk '/=> \//{print $1, $3}' | while read -r name path; do
        if [[ ! "$name" =~ $EXCLUDE ]] && [ ! -e "$TARGET/lib/$name" ]; then
            cp -L "$path" "$TARGET/lib/$name"
        fi
    done
done

echo "Комплект GStreamer: $TARGET ($(du -sh "$TARGET" | cut -f1))"
