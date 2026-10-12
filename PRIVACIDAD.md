# Privacidad

Spotiseek envía algunos datos al creador para poder mejorar la app y evitar usos indebidos. Esta página explica cuáles, para qué y cómo pedir que se borren.

## Qué se envía
Cada vez que se abre la app (y solo si aceptaste el aviso del primer inicio):

| Dato | Para qué |
|---|---|
| Dirección IP | La anota el servidor. Permite identificar usos indebidos |
| Usuario de Soulseek | Identificar la cuenta. **Nunca se envía la contraseña** |
| Identificador de la instalación | Código aleatorio generado en tu equipo |
| Código de tu computadora | Hash (irreversible) de un identificador de Windows; ayuda a que una suspensión no se evada fácilmente |
| Versión de la app y sistema operativo | Saber qué versiones se usan |
| Cantidad de inicios y fecha del último | Estadísticas de uso |

Con la app abierta se repite una consulta breve cada 5 minutos, solo para saber si el acceso fue suspendido; esa consulta no guarda nada nuevo.

## Qué NO se envía
Tu contraseña de Soulseek, tus canciones, tus listas de Spotify, tus descargas ni el contenido de tus carpetas.

## Para qué se usan
- Mejorar la app (qué versiones se usan, cuánta gente la usa).
- Suspender el acceso ante un mal uso. La persona ve el motivo al abrir la app.

No se venden ni se comparten con terceros.

## Dónde se guardan
En una base de datos del creador alojada en [Supabase](https://supabase.com). Solo el creador tiene acceso para leerlos.

## Cómo pedir que se borren tus datos
Contactá al creador por su perfil de GitHub ([@facuuuburgos](https://github.com/facuuuburgos)) indicando tu usuario de Soulseek. No publiques datos personales en un Issue público. Si pedís el borrado, tené en cuenta que quien fue suspendido podría volver a quedar registrado al abrir la app de nuevo.

## Si no estás de acuerdo
Podés no aceptar el aviso del primer inicio: la app se cierra y no se envía nada. También podés desinstalarla.

## Límites de este sistema
Es una medida simple de moderación, no una garantía absoluta: al ser un proyecto de código abierto, alguien con conocimientos podría evadirla.
