import { writeFileSync, mkdirSync, existsSync } from "fs";
import { dirname } from "path";

const API_KEY = process.env.ELEVENLABS_API_KEY;
if (!API_KEY) {
  console.error(
    "Error: falta ELEVENLABS_API_KEY.\nDefinela en promo/.env o exportala: export ELEVENLABS_API_KEY=sk_..."
  );
  process.exit(1);
}

// Bella — voz femenina, profesional y calida. Misma voz usada en el proyecto de visa.
const VOICE_ID = "hpp4J3VqNfWAUOO0d1Us";
const MODEL_ID = "eleven_v3"; // mejor soporte multilingue

// El guion se escribe fonetico donde hay URLs: la locucion dice "punto" y "barra"
// para que se entienda al oido, mientras la pantalla muestra la URL real.
const scenes = [
  {
    id: "01-gracias",
    text: "¡Gracias por unirte a Neexy! Ya tienes acceso a todo nuestro catálogo de películas y series. Te explico cómo empezar.",
  },
  {
    id: "02-dispositivos",
    text: "Puedes ver Neexy en casi cualquier dispositivo: tu celular, tu computadora, tu smart TV, o tu consola.",
  },
  {
    id: "03-firetv",
    text: "Si vas a ver en Fire TV: abre la tienda de aplicaciones, busca Moonfin, e instálala.",
  },
  {
    id: "04-googletv",
    text: "En Google TV es igual: busca Moonfin en la tienda, e instálala.",
  },
  {
    id: "05-roku",
    text: "Y si tienes Roku, también es Moonfin: búscala en la tienda de canales, y añade el canal.",
  },
  {
    id: "06-descargas",
    text: "En tu celular, tablet o computadora, descarga Moonfin desde tu tienda de aplicaciones: Play Store en Android, o App Store en iPhone, iPad y Mac. En Windows, entra a neexy punto net, barra descargar.",
  },
  {
    id: "07-conectar",
    text: "Ya con la aplicación instalada, ábrela. Cuando te pida la dirección del servidor, escribe: te ve punto neexy punto net. Solo se escribe una vez. Luego elige Quick Connect: tu pantalla mostrará un código. Escríbelo en el siguiente paso de esta misma página, la que se abrió cuando compraste tu membresía. Por eso, no la cierres hasta conectar tu tele. Y listo: sin usuario ni contraseña en el control remoto.",
  },
  {
    id: "08-cierre",
    text: "¿Te quedó alguna duda? Entra a neexy punto net, barra blog, en la sección de Guías, donde encontrarás más tutoriales. Ya con esto estás listo. Solo falta que prepares las palomitas para comenzar a disfrutar.",
  },

  // Video corto del primer paso del wizard ("Algunas cosas que debes saber
  // antes de empezar"). Va en su propia carpeta para no mezclarse con el de arriba.
  // La direccion de renovacion no se dice: lleva un ?renovar=1 que al oido no se
  // entiende, y el paso del wizard la deja como enlace debajo del video.
  {
    id: "antes/01-intro",
    text: "Antes de empezar, tres cosas que te van a servir más adelante: cómo renovar, cómo recuperar tu acceso, y cómo cambiar de dispositivo.",
  },
  {
    id: "antes/02-renovar",
    text: "Para renovar, entra al enlace de renovación que está debajo de este video. Escribe tu usuario y tu contraseña, y toca Validar cuenta. Luego elige la duración y el plan, y paga tu suscripción.",
  },
  {
    id: "antes/03-recuperar",
    text: "¿Olvidaste tu usuario o tu contraseña? En neexy punto net, toca Recuperar en el menú de arriba, elige qué necesitas, y te lo enviamos a tu correo.",
  },
  {
    id: "antes/04-dispositivos",
    text: "Tu plan incluye cierto número de dispositivos. Si ya los usas todos y quieres ver Neexy en otro, primero cierra sesión en el que ya no vas a usar: toca tu inicial, arriba a la izquierda, y luego Sign Out.",
  },
  {
    id: "antes/05-cierre",
    text: "Eso es todo. Ahora sí, vamos a instalar la app.",
  },
];

const outputDir = "public/voiceover";

async function generateVoiceover() {
  for (const scene of scenes) {
    const filePath = `${outputDir}/${scene.id}.mp3`;
    mkdirSync(dirname(filePath), { recursive: true });

    // Solo se regenera lo que falta: cada llamada consume creditos de ElevenLabs.
    // Para rehacer una pista, borra su MP3 y vuelve a correr el script.
    if (existsSync(filePath)) {
      console.log(`Ya existe, se conserva: ${scene.id}`);
      continue;
    }

    console.log(`Generando: ${scene.id}...`);

    const response = await fetch(
      `https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}`,
      {
        method: "POST",
        headers: {
          "xi-api-key": API_KEY,
          "Content-Type": "application/json",
          Accept: "audio/mpeg",
        },
        body: JSON.stringify({
          text: scene.text,
          model_id: MODEL_ID,
          voice_settings: {
            stability: 0.55,
            similarity_boost: 0.8,
            style: 0.25, // tutorial: menos dramatico que un promo
          },
        }),
      }
    );

    if (!response.ok) {
      const err = await response.text();
      console.error(`Error en ${scene.id}: ${response.status} - ${err}`);
      continue;
    }

    const audioBuffer = Buffer.from(await response.arrayBuffer());
    writeFileSync(filePath, audioBuffer);
    console.log(`  Guardado: ${filePath} (${(audioBuffer.length / 1024).toFixed(1)} KB)`);
  }

  console.log("\nListo. Ahora medí las duraciones con: npm run durations");
}

generateVoiceover().catch(console.error);
