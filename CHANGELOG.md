# Historial de cambios

## [1.0.3] - 2026-10-11

- **Icono más nítido** en la barra de tareas y en la ventana (versiones propias para cada tamaño). Llega por actualización automática.
- Las actualizaciones ahora también pueden renovar los **iconos e imágenes** de la app, no solo el código.
- Panel de administración con icono propio (rojo) para distinguirlo.

## [1.0.2] - 2026-10-11

**Nuevo**
- **Actualización automática:** al abrir, la app busca una versión nueva y, si la hay, la instala sola y se reinicia. Se quitó el ajuste «Buscar actualizaciones al abrir» (ahora es siempre).
- **Registro de uso y suspensiones.** En el primer inicio se explica y se pide aceptar el envío de la IP, el usuario de Soulseek (nunca la contraseña), la versión, el sistema operativo y un identificador de la instalación y de la computadora (hash). El creador puede suspender el acceso: la persona ve un aviso con el motivo y la app se cierra. Mientras la app está abierta se vuelve a comprobar cada 5 minutos. Ver [PRIVACIDAD.md](PRIVACIDAD.md).
- **Panel de administración** (carpeta `admin/`) para el creador: lista de usuarios, suspensión y reactivación, notas privadas.

## [1.0.1] - 2026-10-11

- Las canciones ahora se guardan por defecto en **Descargas de Windows → Spotiseek** (antes quedaban dentro de la carpeta de instalación). Los audios de la carpeta anterior se mueven solos, una sola vez.

## [1.0.0] - 2026-10-11

Primera versión pública.

- Importa enlaces de Spotify: canciones, álbumes, EPs y playlists.
- Búsqueda y descarga por Soulseek (slskd), con varias descargas en paralelo.
- Prioriza MP3 320 kbps y versiones Extended Mix; pasa al siguiente resultado si uno no responde o se tranca.
- Respaldo opcional con YouTube Music (MP3 320 kbps).
- Selección múltiple, pausar todo y cancelar todo.
- Ventana de Opciones para elegir resultados a mano.
- Archivos de audio con el nombre «Canción - Artista».
- Menú de Ajustes.
- Instalador y desinstalador propios; actualizaciones desde la app.
