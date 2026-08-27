# Changelog

All notable changes to Wizarr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project uses [Calendar Versioning](https://calver.org/).



## [Unreleased]

### Fixed

- **Las URLs absolutas degradaban a `http://` detras del proxy.**
  `/settings/notifications` (sin barra final) respondia **308** hacia
  `http://sauron.neexy.net/settings/notifications/` — esquema http. Con HSTS el
  navegador lo repara antes de emitir la peticion, pero un cliente que no lo
  aplique manda la cookie de sesion en claro.

  La causa no estaba en esa ruta. **Nada traducia `X-Forwarded-Proto`**, asi que
  Flask veia `wsgi.url_scheme == "http"` en toda peticion y construia sobre el
  **todas** sus URLs absolutas: las redirecciones de `strict_slashes` y el
  enlace de restablecimiento que se renderiza en el modal de admin
  (`request.url_root`), que un administrador puede copiar y enviar. El enlace
  **enviado por correo** ya estaba a salvo — `resend_email._public_base_url`
  prefiere la URL publica guardada—; el copiable no.

  **Requiere `TRUSTED_PROXY_COUNT`.** La cabecera la controla quien hace la
  peticion mientras no haya un proxy propio reescribiendola, asi que solo se
  honra cuando la variable declara que lo hay — la misma puerta que
  `auth.routes._client_ip` ya aplicaba a `X-Forwarded-For`, ahora leida de una
  sola definicion (`config.trusted_proxy_count`) porque dos lectores que
  discrepan no serian una inconsistencia sino un fallo de seguridad. **Sin esa
  variable el arreglo no hace nada**, que es el comportamiento correcto en un
  despliegue sin proxy delante.

  Se toma **solo el esquema**. `x_for` se deja en 0 a proposito: reescribiria
  `remote_addr`, que es la clave de todos los limites de tasa, y
  `api_routes.RequestPasswordReset` dimensiono sus topes sin clave **como
  guardia de cuota** precisamente porque la direccion de salida de la tienda es
  compartida. Redefinir eso en silencio no es un efecto secundario aceptable.

## [2026.9.10] (2026-08-26)

### Added

- **Avisos de disputas, contracargos cerrados y avisos tempranos de fraude, con
  la evidencia ya montada.** sauron lleva tiempo sabiendo construir el paquete
  que gana una disputa de bienes digitales (`access_activity_log`, los elementos
  de Visa CE 3.0) y renderizarlo en `/eventos/<id>`, pero nadie se enteraba de
  que hubiera algo que responder hasta que abria la pestana por casualidad. Una
  disputa sin contestar se pierde por omision.

  Ahora `charge.dispute.created`, `charge.dispute.closed` y
  `radar.early_fraud_warning.created` mandan un aviso cada uno, con el plazo, el
  importe, si el caso es elegible para CE 3.0, **cuanta reproduccion respalda la
  respuesta** y enlace directo a la vista de evidencia. Los otros eventos de
  disputa (`updated`, `funds_withdrawn`, `funds_reinstated`) NO avisan: son
  contabilidad sobre una disputa ya conocida, y avisar cuatro veces por un solo
  contracargo es como se consigue que alguien silencie el canal.

  Son eventos **operativos**: llegan a todo agente configurado sin depender de
  una casilla. La suscripcion es opt-in y las filas de agente conservan lo que
  se guardo al crearlas, asi que un evento nuevo nace mudo — inaceptable para
  un plazo con dinero detras.

  sauron sigue sin escribir nada en Stripe. El aviso lo dice explicitamente:
  el envio lo hace una persona, porque solo hay un intento.

### Fixed

- **El aviso se compone DESPUES de correlacionar, no al guardar la fila.** El
  aviso de refunds sale desde dentro de `sync_stripe_events`, antes de que nada
  este correlacionado. Un aviso de disputa ahi habria dicho "sin vincular a
  ninguna cuenta, sin reproducciones" sobre una compra con meses de historial:
  equivocado justo en la direccion que ensena a desconfiar de las alertas.

- **La procedencia del vinculo se registra, en vez de deducirla de la fila.**
  Aqui estaba el error que casi se cuela: la tienda estampa `sauronUserId` en el
  PaymentIntent y ningun id de invitacion, asi que la ruta AUTORITATIVA escribe
  `wizarr_user_id` y deja `invitation_id` en NULL — exactamente el mismo rastro
  que deja el ultimo recurso, que empareja por el correo de facturacion. Leerlo
  de las columnas habria etiquetado **toda disputa real de produccion** como una
  suposicion: el aviso invertido, en la alerta que mas necesita creerse.

- **Un sync manual tambien avisa.** "Sync now" correlacionaba pero no alertaba.
  Como la ingesta es idempotente, una disputa que cayera en un sync pulseado se
  quedaba muda para siempre: el siguiente pase ya la ve como conocida. Los dos
  caminos pasan ahora por `sync_and_correlate`, y un test de fuente impide que
  aparezca un tercero que se lo salte.

- **Un backfill no despierta a nadie.** El primer sync de una instalacion lee 30
  dias hacia atras, y "Re-sync last 30 days" lo hace a peticion. "Recien
  guardado" no es "recien ocurrido": solo se avisa de eventos de menos de 72 h,
  margen suficiente para sobrevivir a un fin de semana caido sin convertir el
  dia uno en una bandeja llena de casos ya resueltos.

## [2026.9.9] (2026-08-26)

### Fixed

- **Los jobs programados se registraban antes de que la base de datos
  existiera, asi que el sync de Stripe no arrancaba NUNCA.**
  `init_extensions` registraba los jobs unas cincuenta lineas por encima de
  `db.init_app(app)`. Cualquier job que lea la BD al registrarse — el sync de
  Stripe lee la llave, el de LDAP lee su configuracion — moria ahi con
  `RuntimeError: The current Flask app is not registered with this 'SQLAlchemy'
  instance`. Empujar un contexto de app no ayuda y el comentario del codigo
  diagnosticaba justo eso mal: falta el engine, no el contexto; el bloque de
  Stripe si empujaba contexto y fallaba igual.

  El sync solo existia porque guardar los ajustes de Eventos lo registra desde
  un request, donde la BD ya esta enlazada. Vivia en memoria hasta el siguiente
  reinicio del contenedor y entonces desaparecia sin dejar rastro. Eso es el
  apagon de dos dias que motivo el vigilante de 2026.9.6.

  El bloque del scheduler pasa a ir despues de `db.init_app` / `migrate.init_app`.
  Ningun test en ejecucion puede cubrir esto — pytest se salta el bloque
  entero — asi que la invariante queda fijada leyendo el propio fuente.

- **El vigilante mataba de hambre al job que vigilaba.**
  `add_job(replace_existing=True)` sin `next_run_time` explicito construye un
  trigger nuevo cuyo `start_date` por defecto es `now + interval`, y APScheduler
  lo adopta. Cada llamada empuja el disparo un intervalo completo al futuro, asi
  que un llamador que corra mas a menudo que el intervalo mueve la porteria mas
  rapido de lo que avanza el reloj y el job no dispara ni una vez. El vigilante
  corria cada 5 minutos contra un job de 15.

  En produccion se vio `minutes_since_sync` subiendo 186 → 191 → 196 → 201 →
  ... un paso por tick, para siempre, con `scheduler_running: true`,
  `job_registered: true` y la pestana anunciando alegremente "Next run" a quince
  minutos vista. La alerta horaria prometia ademas "The job was re-registered
  automatically", una reparacion que esa funcion era incapaz de entregar.

  Ahora un job ya correcto se deja estrictamente en paz. Se re-registra solo si
  falta, si cambio el intervalo guardado, o si esta **pausado**
  (`next_run_time is None`: registrado y nunca va a correr), y en ese caso se le
  pasa `next_run_time` explicito para que corra ya en vez de dentro de un
  intervalo — lo que impide de paso que un reinicio deje un `last_sync` viejo
  del que el vigilante se cuelgue.

  Los 22 tests anteriores no podian verlo: su doble de scheduler hacia
  `next_run_time = now` — "dispara ya" — exactamente lo contrario del real.

- **El banner de sync parado no decia por que.** `check_stripe_sync_health`
  ahora expone `stripe_sync_last_error`, el unico campo que separa "no corre
  nada" de "corre y Stripe lo rechaza". La pestana distingue los dos casos y la
  alerta deja de prometer reparaciones que no hizo.

- **La correlacion de eventos no estaba acotada en tiempo.** El tope de 200
  filas no lo era: cada compra sin resolver cuesta una lectura de PaymentIntent
  de hasta 20 s, asi que un atasco podia pasarse del intervalo de 15 minutos.
  Con `max_instances=1` el tick siguiente no se encola detras, se descarta con
  un WARNING que nadie lee — y el sync vuelve a parecer muerto. Se anade un
  presupuesto de reloj; lo que no entra espera al tick siguiente.

## [2026.9.8] (2026-08-26)

### Fixed

- **La cola de disputas mostraba una fila por evento, no por disputa.** Stripe
  emite hasta cinco eventos por un solo contracargo (`created`, `updated`,
  `closed`, `funds_withdrawn`, `funds_reinstated`), todos con el mismo id de
  disputa y el mismo plazo, y la cola leía las filas tal cual: un contracargo
  aparecía hasta cinco veces y la tarjeta "Disputes" contaba todas las copias.
  Y lo que no era cosmético: nada filtraba por el desenlace, así que una
  disputa ya ganada o perdida seguía en un panel titulado "Disputes awaiting
  response" hasta que se le pasaba la ventana — pidiendo contestar un caso ya
  cerrado. Ahora hay una entrada por disputa, con su evento más reciente, y las
  de desenlace terminal salen de la cola; un estado desconocido no la saca,
  porque el silencio no es un veredicto. Las disputas sin id extraído se
  cuentan por separado en vez de colapsarse, para que un arreglo de
  deduplicación no pueda esconder un contracargo vivo.


## [2026.9.7] (2026-08-26)

### Fixed

- **El badge de "Stripe Refund" seguía ilegible en modo oscuro.** El arreglo de
  2026.9.5 no funcionó. Una variante `dark:` es un candidato propio: declarar
  `bg-amber-900` no produce `dark:bg-amber-900`, y aquel `@source inline` solo
  listaba las utilidades base. Parecía correcto porque cuatro de los cinco
  colores se veían bien, pero de rebote: rojo, azul, verde y morado aparecen
  como `dark:bg-<color>-900` desnudo en algún marcado real y el escaneo los
  generaba igual. Ámbar solo aparece como `dark:bg-amber-900/20` y `/40`, y un
  modificador de opacidad es OTRA clase — mientras que `dark:text-amber-200` sí
  existe desnudo. Fondo claro con texto claro. Ahora las cuatro formas del
  catálogo se declaran explícitamente, incluidas las `dark:`, y
  `tests/test_notification_badges.py` falla si el catálogo y la declaración se
  separan, sin necesitar node ni compilar CSS.


## [2026.9.6] (2026-08-26)

### Fixed

- **Un usuario con guion en el alta quedaba condenado a no renovar nunca.** El
  formulario de invitación compartía validador con las cuentas de admin, así
  que aceptaba guiones, guiones bajos, apóstrofos y puntos. La tienda comprueba
  el usuario contra `^[A-Za-z0-9]{1,15}` antes de renovar una membresía o de
  mandar un enlace de contraseña, de modo que una cuenta como
  `qa-2026-08-26-1` se creaba sin problema y después no podía renovar jamás. Y
  como todos los rechazos de la tienda responden el mismo mensaje genérico a
  propósito, ni el cliente ni soporte podían ver por qué. El alta es el único
  sitio donde se puede impedir. El mínimo sube además de 3 a 7 caracteres: tres
  es un espacio muy pequeño para una cuenta que da a un servidor de medios
  público. Las reglas se enuncian bajo el campo antes del primer envío, en
  español, igual que ya se hacía con la contraseña. Las cuentas con guion que
  ya existen siguen sin poder renovar: esto solo impide que se creen nuevas.

- **El sync programado de Stripe podía morir sin avisar a nadie.** Estuvo dos
  días sin correr con la pestaña Eventos mostrando el sync habilitado, la llave
  correcta y un intervalo de 15 minutos. Esa pestaña es la que expone las
  disputas, y una disputa sin responder se pierde por plazo. Tres silencios lo
  permitían: el registro del job logueaba a nivel *debug* culpando a las
  migraciones, un scheduler que no arrancaba solo emitía un *warning* (y con él
  mueren todos los jobs, vencimientos incluidos), y la pantalla de ajustes se
  rendía en silencio si el scheduler no corría — guardar una llave válida
  contestaba "Settings saved" y no cambiaba nada. Ahora hay un vigilante colgado
  de `/health`, que es lo único que corre solo: el HEALTHCHECK del contenedor lo
  llama cada 30 segundos mire alguien la pestaña o no. Detecta los tres estados
  de "no va a correr nada", intenta repararlos y manda un aviso operativo (que
  llega a todos los agentes, sin depender de suscripción) como máximo una vez
  por hora. La pestaña muestra además el estado del scheduler y el próximo
  disparo, para poder cerrar la causa raíz en vivo sin acceso a los logs. El
  vigilante respeta `WIZARR_DISABLE_SCHEDULER` y `FLASK_SKIP_SCHEDULER`: en un
  despliegue que corre sin scheduler a propósito, no avisa ni intenta arrancarlo.


## [2026.9.5] (2026-08-25)

### Fixed

- **El badge de "Stripe Refund" salía ilegible en modo oscuro.** Al mover los
  colores de los badges al catálogo de eventos, las clases pasaron de vivir en
  una plantilla a vivir en un `.py`, y Tailwind no escanea Python. Las
  variantes claras sobrevivieron porque ya existían en otro marcado, pero
  `dark:bg-amber-900` y `dark:text-amber-200` no existían en ningún sitio y
  dejaron de generarse — ámbar pálido sobre ámbar pálido. Ámbar fue el único
  color estrenado, por eso fue el único que se rompió. Ahora las clases se
  declaran con `@source inline`, que no depende de heurísticas de extensión.


## [2026.9.4] (2026-08-25)

### Fixed

- **Un Jellyfin inalcanzable borraba y apagaba bibliotecas, en silencio y para
  siempre.** `JellyfinClient.libraries()` atrapaba cualquier excepción y
  devolvía `{}`, con lo que un servidor caído quedaba indistinguible de uno que
  de verdad no tiene bibliotecas. `scan_all_server_libraries`, que corre en
  CADA arranque, lee ese `{}` como "desaparecieron todas": borra las que
  ninguna invitación referencia, apaga el resto y comitea. El
  `except`/`rollback` exterior nunca disparaba, porque `libraries()` no lanzaba
  nada. Reproducido con dos bibliotecas y Jellyfin lanzando `ConnectionError`:
  una borrada, la otra apagada, `errors=[]`. Y como por diseño el scan no
  vuelve a encender lo apagado, el daño era permanente hasta re-marcarlas a
  mano. Aguas abajo eso deja a una tienda sin bibliotecas que conceder.
  Ahora `libraries()` lanza, y el scanner se niega a correr el paso destructivo
  cuando la respuesta viene vacía habiendo filas en la base. La guarda vive en
  el scanner y no en el cliente porque emby, komga y audiobookshelf se tragan
  su error igual.

- **`user_renewed` no le habría llegado a ningún agente existente.**
  `notification_events` guarda opt-INs y cada fila conserva lo que se guardó al
  crearla, así que un evento nuevo es invisible para toda base anterior. Se
  corrige con una migración de backfill, no marcándolo operativo: una
  renovación es rutina que un operador puede querer apagar.

### Added

- **Alertas operativas.** Cuatro eventos nuevos: `library_scan_failed`,
  `libraries_disabled_by_scan`, `stripe_refund` y `user_renewed`. El pipeline
  de Stripe ya clasificaba refunds y disputas pero era mudo — nada llamaba a
  `notify()`. Los refunds avisan solo por las filas escritas en esa pasada: el
  sync corre por intervalo sobre una ventana móvil, y avisar por cualquier cosa
  que no sea un insert nuevo re-anunciaría el mismo refund cada pocos minutos.
  Las renovaciones no pueden salir de Stripe (aquí son cargos sueltos, no
  suscripciones), así que van enganchadas a `POST /api/users/<id>/extend`.

### Changed

- **Un solo catálogo de tipos de evento.** Cada tipo estaba escrito a mano en
  ocho puntos repartidos por seis archivos, y olvidar cualquiera producía un
  evento que no le llegaba a nadie sin que nada lo delatara. Ahora todo sale de
  `EVENT_TYPES`. Trae además la clase de eventos "operativos", que saltan el
  filtro de suscripción y llegan a todos los agentes, porque la suscripción es
  opt-in y una alerta operativa nueva nacía muda.


## [2026.9.3] (2026-08-25)

### Fixed

- **El tope sin clave de `password-reset-request` era agregado, no por
  comprador.** Salió en 2026.9.2 documentado como "tope por IP, 10/hora", y esa
  lectura es falsa. `key_func` por defecto es `get_remote_address`, o sea
  `request.remote_addr`, y quien llama es una tienda haciendo una petición
  servidor-a-servidor: todos los compradores del mundo llegan desde el mismo
  puñado de direcciones de salida. Así que ese contador no aisla a un abusador
  de un cliente legítimo — los suma, y el número 11 de la hora se queda sin
  reseteo sin importar quién sea.

  Ahora esos topes están dimensionados como lo que de verdad son, un guardián de
  cuota: 100/día (que es literalmente el free-tier de Resend) y 30/hora, para
  que una ráfaga no se coma la asignación del día entera antes de que nadie lo
  note. El control que sí separa a una persona de otra sigue siendo el que va
  por username —3/hora y 10/día, leídos del cuerpo—, y limitar por IP del
  comprador le toca a la tienda, que es el único sitio donde esa dirección se
  ve.

  `verify-credentials` tiene la misma forma (20/hora sin clave) y queda igual a
  propósito: ahí cada intento cuesta un login de Jellyfin, no cuota de correo,
  y ensanchar un tope de una función de seguridad distinta no es algo que deba
  colarse en este cambio.

## [2026.9.2] (2026-08-25)

### Added

- **`POST /api/users/password-reset-request`: la puerta que le faltaba a
  "olvidé mi contraseña".** 2026.9.1 dejó lista la entrega
  (`send_password_reset_email`) pero solo como función de Python, alcanzable
  únicamente desde dentro del proceso — el modal de admin. La tienda solo tiene
  un username y ninguna forma de llamarla. Esta ruta es esa forma.

  Lo más cercano que ya existía, `POST /api/users/<id>/reset-password`, no
  servía: acuña el token y devuelve la ruta, pero no manda nada, pide el `id`
  numérico y no toca ni el registro de envíos ni los contadores de cuota.

  Decisiones que vale la pena dejar escritas:

  * **Siempre responde `200 {"accepted": true}`.** Usuario inexistente, cuenta
    sin correo, Resend apagado, Resend rechazando el envío, dos cuentas con el
    mismo nombre — todo idéntico desde afuera. El formulario que la consume es
    público y sin autenticar, así que cualquier diferencia en la respuesta es un
    oráculo gratis de "¿existe esta cuenta?". `accepted` dice que se recibió la
    petición, **no** que se haya enviado un correo. El resultado real va al log
    y a Activity > Resend, que es donde un operador puede actuar.

    La única excepción es un cuerpo malformado (400): eso es un bug del que
    llama y no dice nada de ninguna cuenta.

  * **El cuerpo es uniforme; el reloj no.** Un acierto escribe en la base y
    habla con Resend; un fallo vuelve tras un `SELECT`. Cerrar esa diferencia le
    toca a quien llama —neexy sostiene toda respuesta hasta un piso fijo—, y lo
    que la acota aquí es el tope por IP, porque separar esas dos distribuciones
    exige muchas muestras. Para poder dimensionar ese piso con datos y no a ojo,
    `send_email` ahora registra `elapsed_ms`.

  * **El tope por IP es 10/hora, más estricto que los 20 de
    `verify-credentials`.** Cada llamada aquí gasta cuota real de Resend (100 al
    día en el plan gratis) y mete correo en la bandeja de alguien. El radio de
    una inundación es una cuota quemada más un cliente spameado, no CPU
    desperdiciada. Los topes por usuario (3/hora, 10/día) sí son espejo.

  * **Un username ambiguo no le manda correo a nadie.** sauron guarda una fila
    por servidor, y un token de reseteo pertenece a UNA fila y cambia la
    contraseña solo en ese servidor. Elegir una al azar resetearía una cuenta
    que quizá no era la pedida y dejaría la otra intacta. Se rechaza y se
    registran los ids para que el operador mande el enlace a mano.

  * **Pedir un reseteo invalida el enlace anterior**, porque
    `create_reset_token` marca como usados los tokens sin usar del usuario. Eso
    lo hereda esta ruta: alguien puede invalidar el enlace que el dueño tiene en
    la mano. No puede *leerlo* —la entrega siempre va al correo registrado—, así
    que el peor caso es que el dueño tenga que pedirlo otra vez, y los topes son
    lo que impide que eso sea una molestia usable.

  * **`no_email` solo deja rastro en el log.** `send_password_reset_email`
    vuelve antes de `send_email`, así que no se escribe fila en `resend_email` y
    el caso no aparece en Activity > Resend. Es el caso que de verdad ocurre
    (filas viejas importadas antes de que el correo fuera obligatorio), así que
    se registra como `warning` con el id del usuario.

## [2026.9.1] (2026-08-25)

### Added

- **Envío de correo por Resend, y con él la vía para "olvidé mi contraseña".**
  sauron ya sabía generar tokens de reseteo (`password_reset_token`) y ya servía
  `/reset/<code>`, pero no tenía forma de poner ese enlace delante del usuario:
  había que copiarlo a mano desde el modal de admin y mandarlo por fuera. Esa
  era la pieza que faltaba para el botón de "olvidé mi contraseña" que viene
  después — el enlace existía, la entrega no.

  Nueva pestaña **Activity > Resend** con la configuración, el estado y el
  registro de envíos, y `send_password_reset_email(user)` como única función que
  ese futuro formulario público tendrá que llamar.

  Decisiones que vale la pena dejar escritas:

  * **Se llama a la API REST con `requests`, no con el SDK `resend`.** Un POST a
    un endpoint no justifica una dependencia, y así el fallo se ve como lo que
    es (código de estado + `name` del error) en vez de envuelto en excepciones
    del SDK. Mismo criterio que `stripe_events`.

  * **La restricción real del free-tier no es la cuota, es el dominio.** Resend
    solo envía desde un dominio verificado (uno en el plan gratis). Sin
    verificar, el único remitente que funciona es `onboarding@resend.dev` y
    *solo entrega al correo del dueño de la cuenta de Resend*; cualquier otro
    destinatario se rechaza. Es decir: se puede guardar la clave, ver la
    pestaña en verde y no poder mandarle un reseteo a un solo usuario. Por eso
    el botón de envío de prueba no es un extra — es lo único que demuestra que
    el dominio está verificado — y por eso guardar con remitente `resend.dev`
    sale en ámbar y no en verde.

  * **La URL pública es un ajuste propio, no `request.url_root`.** Detrás del
    proxy inverso de TrueNAS lo que sauron ve suele ser una dirección interna:
    el enlace le funciona al admin que lo generó y a nadie que lo reciba.

  * **No se consulta el estado de entrega.** `GET /emails/{id}` devuelve
    `restricted_api_key` (401) con una clave de solo envío, y el free-tier borra
    los datos a los 30 días. El resultado del envío es la única foto honesta y
    duradera, así que se guarda en `resend_email` — que además es de donde salen
    los contadores de cuota, porque Resend no expone un endpoint de uso.

  * **Los tres 429 se distinguen.** `daily_quota_exceeded`,
    `monthly_quota_exceeded` y `rate_limit_exceeded` significan cosas distintas
    y cada uno lleva su propio consejo; el mensaje de Resend se muestra tal cual
    en el registro, porque un error de proveedor parafraseado es un ticket de
    soporte que nadie puede responder.

- **Botón "Enviar" en el modal de reseteo de contraseña.** Manda por correo el
  token que ya está en pantalla en vez de acuñar uno nuevo: `create_reset_token`
  invalida los tokens sin usar anteriores, así que generar otro aquí dejaría al
  admin mirando un enlace muerto mientras al usuario le llega uno distinto. Si
  la cuenta no tiene correo, lo dice en lugar de ofrecer un botón que fallaría.

## [2026.8.10] (2026-08-25)

### Fixed

- **La correlación de evidencia por fin puede resolver.** La pestaña Eventos
  existe para pegar el historial de reproducción de sauron a una disputa de
  Stripe, y ese enganche nunca funcionó: sauron leía `wizarrInvitationId` sobre
  `PaymentIntent.metadata`, mientras la tienda mandaba `orderToken` sobre
  `CheckoutSession.metadata` — objeto **y** clave equivocados. Todos los
  PaymentIntents revisados traían `metadata: {}`.

  neexy ya movió su metadata al PaymentIntent y manda el id de usuario directo
  (`{"orderId": …, "sauronUserId": …}`). sauron ahora lo lee, así que
  `sauronUserId` → `StripeEvent.wizarr_user_id`.

  Se conserva `wizarrInvitationId`: hoy nadie lo manda, pero una invitación es
  un enlace más rico que un id de usuario suelto — llena `invitation_id` y el
  usuario se sigue derivando de ella.

### Changed

- **El orden de resolución ahora es explícito y es carga estructural.** Las tres
  fuentes se prueban de más fuerte a más débil: metadata del PaymentIntent →
  evento hermano con `invitation_id` → email del checkout. El email es una
  conjetura (es el de **facturación**, que no tiene por qué ser el que canjeó la
  invitación); si corriera primero contestaría por **toda la compra** y cada
  evento posterior reutilizaría esa conjetura sin llegar nunca a leer la
  metadata autoritativa. Por lo mismo, el reuso de hermanos solo acepta enlaces
  con `invitation_id`: un `wizarr_user_id` suelto pudo venir del email.

- **Una lectura de PaymentIntent por compra, no por evento.** Consultar la
  metadata para cada evento convertiría una compra de cinco eventos en cinco
  llamadas idénticas a Stripe; `resolve_pending_links` ahora comparte un caché
  por lote. Los fallos también se cachean, para no reintentar una llave incapaz
  de leer PaymentIntents una vez por evento.

- **Cobertura de la rama que nunca tuvo ninguna.** El único test de correlación
  pasaba `api_key=None`, que **salta por completo** la lectura del
  PaymentIntent — por eso la suite llevaba en verde desde siempre contra un
  contrato que no existía. Añadidos tests que sí ejercitan esa ruta: resolución
  por `sauronUserId`, precedencia sobre el email cuando apuntan a personas
  distintas, id inexistente que cae al email, la ruta por invitación, y que un
  lote lee cada PaymentIntent una sola vez. Verificado que tres de ellos fallan
  sin el lector nuevo.

## [2026.8.9] (2026-08-24)

### Fixed

- **El arreglo de 2026.8.7 rompía el disable por completo.** La línea añadida
  entonces llamaba a `db.session.in_nested_transaction()`, pero `db.session` es
  un `scoped_session` y **no proxea ese método**: lanzaba
  `AttributeError: 'scoped_session' object has no attribute
  'in_nested_transaction'`. El `except` de `_set_user_enabled_state` lo tragaba
  y devolvía `False`, o sea "el disable falló".

  Efecto real, verificado en el servidor de producción: **ningún disable
  funcionó desde 2026.8.7** — ni el barrido, ni el botón de la GUI, ni la API.
  Y como hasta 2026.8.8 un disable fallido escalaba a borrado, *toda* cuenta que
  venciera en esa ventana se borraba. Un fallo intermitente se convirtió en uno
  del 100%.

  Se corrige llamando a `db.session()` para obtener la `Session` real, que sí
  expone `in_nested_transaction()`.

### Changed

- **Cobertura de la interacción que dejó pasar ambos fallos.** Toda la suite
  mockeaba `disable_user` entero, así que el cuerpo de `_set_user_enabled_state`
  **nunca se ejecutaba en los tests** — por eso dos bugs distintos llegaron a
  producción en verde, el segundo en la mismísima línea escrita para arreglar el
  primero.

  Añadidos tests que solo falsean el cliente HTTP y ejercitan de verdad
  `_set_user_enabled_state` (dentro de un savepoint ajeno y como ámbito
  externo) más el barrido completo de punta a punta. Verificado que los tres
  fallan con el código de 2026.8.8, reproduciendo el `AttributeError` exacto de
  producción.


## [2026.8.8] (2026-08-24)

### Fixed

- **Un disable fallido ya no escala a borrado.** El arreglo de 2026.8.7 cubrió
  el caso "el disable funcionó pero la contabilidad transaccional explotó". No
  cubrió el caso de que el disable **falle de verdad**: un 403, un 404 por
  identificador equivocado, un timeout. En cualquiera de esos, el barrido
  seguía borrando la cuenta del cliente.

  Verificado en producción: con `Expiry Action = "Disable User"` configurado,
  dos cuentas vencidas (`user3`, `user4`) fueron **borradas** de Jellyfin por
  corridas de 2026.8.7.

  Ahora el borrado ocurre por **una sola razón**: que el admin haya elegido
  explícitamente `"delete"`. Cualquier otro desenlace — servidor incapaz de
  deshabilitar, disable rechazado, excepción — deja la cuenta **intacta**,
  revierte el savepoint (para no acumular una fila `ExpiredUser` por corrida) y
  registra un error para que el siguiente barrido lo reintente. Una cuenta
  habilitada de más es un problema de facturación que se autocorrige en 15
  minutos; una cuenta borrada es pérdida irreversible de un cliente que paga.

  En el camino de fallo **no** se toca `user.is_disabled`: esa columna es el
  filtro del propio barrido, así que marcarla tras un disable fallido excluiría
  para siempre a una cuenta que sigue habilitada en el servidor de medios.

- **El mismo fallo estaba en dos endpoints de la API, no solo en el barrido.**

  - `POST /api/users/<id>/disable`: si el disable fallaba, **borraba** la cuenta
    y respondía **200** con un mensaje de éxito. El llamador no podía distinguir
    "deshabilitado" de "destruido". Ahora devuelve **502** y no borra nada.
  - `POST /api/invitations/<id>/disable-users`: misma escalada. Ahora los fallos
    se reportan en un array `failed` y la cuenta queda intacta. `count` sigue
    siendo el número de cuentas realmente **deshabilitadas**, así que un cliente
    viejo que solo lea `count` sigue leyendo un número cierto.

  Importa porque es la ruta que usa neexy para revocar por reembolso y
  chargeback: un disable fallido borraba la cuenta del cliente mientras neexy
  registraba "deshabilitado".

### Changed

- Los tests `test_genuine_disable_failure_still_falls_back_to_deletion` y
  `test_disable_raising_still_falls_back_to_deletion` **afirmaban el borrado
  como comportamiento correcto** — fijaban justo el invariante equivocado, y
  por eso 2026.8.7 pasó en verde con el fallo todavía dentro. Sustituidos por
  tests que exigen que la cuenta sobreviva, que `is_disabled` siga en `False` y
  que no quede fila `ExpiredUser`. Se añadió cobertura del caso "ajuste en
  disable + servidor incapaz", y se conservó el único camino que sí borra.


## [2026.8.7] (2026-08-24)

### Fixed

- **Deshabilitar una cuenta vencida ya no la borra.** Con
  `Expiry Action = "Disable User"`, el barrido de expiración deshabilitaba la
  cuenta correctamente en Jellyfin y **acto seguido la borraba**. El disable
  nunca falló: lo que reventaba era la contabilidad de la transacción.

  `disable_user()` hace `db.session.commit()`, lo cual **cierra el savepoint**
  que abrió `disable_or_delete_user_if_expired()`. El `savepoint.commit()`
  posterior lanzaba `ResourceClosedError`, y el `except` lo interpretaba como
  "el disable falló" y ejecutaba el borrado de respaldo.

  Ahora `_set_user_enabled_state` hace `flush` cuando corre dentro de un
  savepoint ajeno y solo commitea cuando es el ámbito externo; el barrido
  resuelve si el disable funcionó **antes** de tocar la transacción, y el
  fallback a borrado exige evidencia positiva de fallo — borrar es
  irreversible y no puede dispararlo un error de savepoint.

- **El barrido vuelve a procesar todos los usuarios vencidos, no solo el
  primero.** El mismo `ResourceClosedError` escapaba del manejador por usuario
  (el `rollback` lanzaba un segundo error) y mataba el job programado. Se
  procesaba **una** cuenta por corrida de 15 minutos y el resto se acumulaba en
  una cola invisible.

  Nota de origen: el patrón de savepoints viene de upstream (`5c9b6d93`), donde
  `delete_user()` también commitea — así que las instancias upstream comparten
  el fallo del job, aunque sin la pérdida de datos, que la introdujo este fork
  en `d52b612b`.

## [2026.8.6] (2026-08-24)

### Added

- **API de renovación autoservicio.** Tres cambios para que un checkout público
  pueda renovar una cuenta existente en vez de emitir siempre una invitación
  nueva.

  `POST /api/users/verify-credentials` comprueba el usuario y la contraseña de
  una cuenta de medios: es la prueba de propiedad que se exige antes de cobrar.
  **Siempre responde 200 con la misma forma** — usuario inexistente, contraseña
  incorrecta, cuenta deshabilitada y servidor no soportado son indistinguibles
  desde fuera. Jellyfin distingue esos casos con 401 y 403, y esa diferencia es
  un oráculo de enumeración de usuarios; colapsarla es justamente el objetivo.

  Dos comportamientos de Jellyfin obligaron a que esto no sea un wrapper de tres
  líneas (leídos de `Jellyfin.Server.Implementations/Users/UserManager.cs`):

  - **Una cuenta deshabilitada no puede autenticarse.** Lanza `SecurityException`
    antes siquiera de revisar la contraseña, y como las cuentas vencidas quedan
    deshabilitadas, son exactamente las que no podrían demostrar propiedad. Se
    habilitan durante la comprobación y se restauran después, bajo un lock por
    cuenta y restaurando desde la columna `user.is_disabled` de sauron y no de
    una lectura viva de la Policy: con un proceso gunicorn y 8 hilos, dos
    comprobaciones simultáneas de la misma cuenta se pisan y pueden dejarla
    habilitada sin que nadie haya pagado.
  - **Los intentos fallidos bloquean la cuenta.** Jellyfin la deshabilita al
    llegar a `LoginAttemptsBeforeLockout`, así que un formulario público sin
    tope es un arma de bloqueo remoto contra clientes que pagan. Se detecta el
    bloqueo propio y se revierte, con límites de 3/hora y 10/día por usuario más
    20/hora por IP. El contador de Jellyfin no tiene API de reseteo y sólo se
    pone a cero con un login exitoso, así que los límites acotan la velocidad y
    una comprobación correcta es lo que realmente sana una cuenta sondeada.

- `POST /api/users/<id>/max-sessions` aplica el límite de streams simultáneos a
  una cuenta **ya existente**. Upstream sólo lo aplica al canjear una invitación,
  lo que sirve para una primera compra pero no para una renovación: quien sube
  de plan ya tiene cuenta, y sin esto se cobra "4 dispositivos" y se deja el
  límite viejo.

### Fixed

- **`POST /api/users/<id>/enable` devolvía 200 aunque el enable fallara**, con un
  mensaje `"Enable failed or not supported"`. Un cliente de la API no podía
  distinguir una cuenta reactivada de una que seguía deshabilitada, así que una
  renovación pagada se reportaba como entregada mientras el comprador no tenía
  acceso. Ahora responde 502.

- Al reactivar un usuario se limpia su registro obsoleto en `ExpiredUser`, para
  que un cliente renovado deje de aparecer bajo "expired users" en el admin.


## [2026.8.5] (2026-08-24)

### Fixed

- **Tailwind nunca escaneó las plantillas de Activity, así que la mitad de sus
  estilos no existían.** El blueprint de Activity tiene su propia carpeta de
  plantillas (`template_folder="../templates"` → `app/activity/templates`), y
  los `@source` de `style.css` solo cubrían `app/templates/**`. Cualquier clase
  usada **únicamente** en la pestaña Activity jamás se generaba y no hacía nada
  — en silencio, sin error ni en build ni en runtime. Llevaba así desde que
  existe la pestaña: `dark:bg-amber-900/10`, `dark:border-red-800/60`, `h-3.5`,
  `xl:grid-cols-6` y una veintena más estaban muertas.

  Se notó ahora porque el panel «Last sync result» emparejó un fondo inexistente
  (`dark:bg-gray-900/40`) con un color de texto que sí existía: en modo oscuro el
  panel se quedaba claro y los números salían blancos sobre blanco, invisibles.
  Añadido el `@source` que faltaba; el build de Docker ya regenera `main.css`,
  así que se arregla ese panel y de paso todo lo demás que llevaba tiempo
  degradado.

## [2026.8.4] (2026-08-24)

### Fixed

- **Cambiar la llave de Stripe no rearmaba el backfill, y el fallo era mudo.**
  `stripe_last_sync_at` marca una posición en el stream de eventos de **una**
  cuenta. Al guardar una llave distinta esa marca se conservaba, así que sauron
  solo le pedía a la cuenta nueva los eventos posteriores al último tick de la
  cuenta vieja: sus 30 días de historia no se leían nunca. La pestaña se quedaba
  vacía mientras cada sincronización reportaba éxito. Ahora guardar una llave
  distinta (o borrarla) resetea la marca, y una migración la limpia una vez al
  actualizar — sin eso, una instancia ya envenenada seguiría reanudando desde la
  posición mala y el arreglo parecería no funcionar.
- **Un solo evento ilegible descartaba el lote entero y aun así reportaba
  éxito.** El `rollback()` dentro del bucle de ingesta tiraba también las filas
  ya *flusheadas* del mismo lote, mientras el contador las seguía contando: la
  sincronización decía "2 eventos nuevos guardados" y guardaba uno. Ahora cada
  evento va en su propio SAVEPOINT, así que un evento malo cuesta exactamente un
  evento, y los que no se pudieron escribir se reportan aparte.

### Added

- **La pestaña explica lo que la sincronización vio de verdad.** «Sin eventos
  nuevos» tenía dos causas opuestas que se renderizaban idénticas: "todo lo que
  vi ya estaba guardado" (sano) y "vi eventos pero ninguno de un tipo que esta
  integración pueda producir" (llave apuntando a otra cuenta). Ahora cada
  desenlace tiene su propia frase, con un tercer estado ámbar que no es ni éxito
  ni error, y un panel «Last sync result» con lo devuelto por Stripe, lo
  monitoreado, lo nuevo, lo ya conocido, la ventana leída, el reparto
  test/live y el histograma de tipos ignorados (`account.updated ×12`…). El
  panel se guarda, así que una corrida **programada** es tan inspeccionable como
  una que hiciste a mano — antes la única forma de distinguir los casos era leer
  el log del contenedor.
- **Badge de modo de la llave.** Una llave enmascarada esconde a qué cuenta
  apunta. El prefijo (`_test_` / `_live_`) se lee sin llamada a la API y sin
  permisos, y una llave restringida puede no tener acceso a `GET /v1/account`
  justo cuando más falta hace saberlo.
- **Botón «Re-sincronizar 30 días»**: ignora la posición guardada y relee toda
  la ventana de retención de Stripe. Idempotente — el índice único sobre
  `stripe_event_id` convierte la relectura en un no-op.

## [2026.8.3] (2026-08-24)

### Fixed

- **La pestaña Eventos no daba señales de vida al configurarla.** Guardar y
  «Sync now» hacían `flash()` + redirect, pero **nada en esta app renderiza
  `get_flashed_messages`** — así que el mensaje, incluido un error de Stripe
  como "401: revisa la llave restringida", era literalmente invisible. Encima el
  redirect caía en la pestaña Dashboard, no en Eventos. El resultado combinado:
  pegabas la llave, no pasaba nada visible, volvías a Eventos y la veías vacía
  sin ninguna explicación. Ambas acciones ahora vuelven a dibujar la pestaña en
  el sitio, con el resultado arriba y el error de Stripe textual.
- **El modo por defecto dejaba vacía una cuenta solo-sandbox.** La pestaña abría
  siempre en Live; con eventos únicamente de prueba, una sincronización exitosa
  se veía idéntica a una rota. Ahora, si no hay tráfico live pero sí de prueba,
  abre en Test — y el selector muestra cuál ganó, así que no se oculta nada.
- El mensaje de sincronización distingue los tres casos que antes se veían
  igual: eventos nuevos guardados, eventos ya conocidos, y cero eventos
  devueltos por Stripe (con el aviso de que la llave quizá no es `rk_test_`).

## [2026.8.2] (2026-08-23)

### Added

- **Pestaña «Eventos» en Activity**: espejo de los eventos de Stripe con la
  evidencia que solo esta instancia puede producir. Stripe sabe que hubo un
  pago; la tienda sabe que se emitió una invitación; ninguno sabe si el
  comprador **usó** el servicio. Eso vive en `ActivitySession` — qué reprodujo,
  cuándo, desde qué IPs y dispositivos — y para una disputa de bienes digitales
  es el artefacto decisivo, el que Stripe llama `access_activity_log`.
- **Ingesta por polling, no webhook.** sauron no es endpoint de webhook a
  propósito: ese es de la tienda, que es quien cumple los pedidos. Meterlo en la
  ruta de entrega de Stripe significaría un segundo secreto, una ruta pública en
  una instancia auto-hospedada, y que el uptime de sauron aparezca como
  "endpoint failing" en el dashboard de otro. `GET /v1/events` además no está
  sujeto a la lista `enabled_events` del webhook (hoy limitada a 3 tipos), y
  rellena los 30 días de retención en la primera corrida: la pestaña nace
  poblada en vez de vacía.
- **Solo lectura.** La llave esperada es restringida y de solo lectura; nada
  escribe a Stripe. El clic que mueve dinero o envía evidencia se queda en el
  dashboard de Stripe, donde hay confirmaciones y rastro de auditoría.
- **19 tipos monitoreados**, encabezados por `radar.early_fraud_warning.created`
  — el primitivo de *dispute deflection*: reembolsar dentro de la ventana evita
  el chargeback entero, sin fee y sin impacto en la tasa de disputas. Quedan
  fuera `invoice.*` y `customer.subscription.*` (la tienda vende Checkout
  Sessions de pago único) y `checkout.session.async_payment_*` (OXXO descartado;
  solo tarjeta): nunca podrían dispararse.
- **Cola de acción** ordenada por fecha límite de respuesta, porque una disputa
  sin contestar es una disputa perdida por omisión.
- **Elementos de Visa CE 3.0** (IP de compra, dispositivo, account ID, email) y
  marca del reason code 10.4, el elegible para contrarespuesta.
- Un evento sin correlacionar se muestra **como** no correlacionado, nunca se
  oculta: el operador necesita saber que falta el enlace justo cuando cae una
  disputa.

### Changed

- La correlación Stripe ↔ sauron es determinista vía el id de invitación que la
  tienda sella en el PaymentIntent, con respaldo por email — que solo se acepta
  si es inequívoco, porque identifica a una **persona** y no a una **compra**.
  Visa CE 3.0 exige resolución por transacción, así que el email solo no basta
  para un cliente recurrente, que es justo el más defendible.

## [2026.8.1] (2026-08-23)

### Fixed

- **El token CSRF caducado ya no deja al usuario en una página muerta**: nada
  configuraba `WTF_CSRF_TIME_LIMIT`, así que regía el default de una hora de
  Flask-WTF, y sin handler de `CSRFError` Werkzeug servía su 400 crudo. Quien
  abría el formulario, iba a buscar el correo con su invitación y volvía,
  recibía "Bad Request" sin marca, sin explicación y con el código que acababa
  de pagar perdido.
- `WTF_CSRF_TIME_LIMIT = None`: el token deja de caducar por reloj propio y vive
  lo que viva la sesión que lo emitió, todavía detrás de cookies `SameSite=Lax`
  y `HttpOnly`.
- El handler de `CSRFError` devuelve el formulario con token nuevo, el código de
  invitación intacto y un aviso legible. Usuario y correo se conservan; las
  contraseñas nunca se repueblan. El handler no aprovisiona, no reserva la
  invitación y no toca el servidor de medios.
- La búsqueda de la invitación en esa recuperación usa igualdad exacta, nunca
  `LIKE`: corre antes de que CSRF esté establecido, así que el código enviado es
  entrada hostil y un `%` habría devuelto un código real y pagado a un llamante
  anónimo.

### Removed

- **Rutas de alta huérfanas** (`/jf/join`, `/emby/join`, `/abs/join`,
  `/kavita/join`, `/komga/join`, `/romm/join`): eran un segundo endpoint público
  de alta que ninguna plantilla ni JS referenciaba, sin rate limit, sin
  `login_required` y sin pasar por `try_claim_invitation`, de modo que la carrera
  de replay de invitaciones de un solo uso seguía abierta ahí. Se conservan
  `/scan` y `/scan-specific`, que sí se usan y exigen sesión de administrador.

## [2026.7.15] (2026-08-23)

### Fixed

- **La reserva de la invitación ya no la invalida a sí misma**: desde 2026.7.13,
  el primer canje legítimo de cualquier código de un solo uso fallaba con
  "Invitation has already been used." sin llegar a crear la cuenta.
  `try_claim_invitation` reservaba la invitación escribiendo `used = True` antes
  de aprovisionar, pero `used` ya significaba "consumida" para `is_invite_valid`,
  que los siete clientes de media server vuelven a llamar desde dentro de
  `_do_join`. Afectaba al 100% de las invitaciones limitadas por
  `/invitation/process`; las ilimitadas no se veían afectadas.
- La reserva pasa a vivir en las columnas nuevas `claimed_at` / `claim_token`.
  `used` vuelve a significar sólo "cuenta creada de verdad" y lo escribe
  únicamente `mark_server_used`, de modo que `_do_join` conserva su validación y
  con ella la defensa en profundidad de los caminos que llaman `client.join()`
  sin reserva.
- Una reserva de más de 15 minutos se considera abandonada y puede volver a
  tomarse. La comprobación va en el `WHERE` del propio claim, en lectura: un
  worker que muera aprovisionando no deja varada una invitación pagada, y no
  hace falta scheduler ni cron.
- `_result_provisioned_anything` trataba cualquier estado distinto de `FAILURE`
  como aprovisionamiento, así que `OAUTH_PENDING` y `AUTHENTICATION_REQUIRED`
  conservaban la reserva y quemaban la invitación a media sesión.
- `release_invitation_claim` escribía `used = False` sin condición y podía
  borrar un `used = True` legítimo puesto por `mark_server_used`.
- Un código reservado deja de reportar "ya fue usada" y explica que está siendo
  canjeado en ese momento.
- Nuevos tests: `test_invitation_claim_collision.py` recorre la pila real
  (manager → `FormBasedWorkflow` → `JellyfinClient._do_join`) y stubbea sólo la
  capa HTTP. Los tests previos mockeaban `WorkflowFactory.create_workflow` y por
  eso ninguno podía detectar este fallo.

## [2026.7.14] (2026-08-07)

### Changed

- **Rate limiting exacto sin Redis**: gunicorn pasa de 4 workers `sync` a un
  único worker `gthread` con 8 hilos. `memory://` cuenta por proceso, así que
  los 4 workers multiplicaban por 4 todo límite declarado y `scaled_limit` sólo
  lo compensaba por aproximación; los hilos comparten memoria, de modo que un
  solo proceso deja los límites exactos — `/login` a 10/min es 10/min. Se
  descartó Redis porque la base de datos es SQLite sobre volumen montado, lo
  que ya impide correr varias réplicas: sería un contenedor y una dependencia
  extra a cambio de exactitud que los hilos dan gratis. La vía de Redis sigue
  abierta vía `RATELIMIT_STORAGE_URI` si alguna vez se sale de SQLite.
  Hilos, y no un worker `sync` a secas, porque uno solo sirve una petición a la
  vez: una llamada lenta a Jellyfin bloquearía toda la app.
- `pool_size` de SQLAlchemy sube a 20 (+10 overflow). Con un solo proceso el
  pool ya no se reparte entre 4: lo comparten los 8 hilos de petición, el
  `ThreadPoolExecutor(max_workers=10)` de monitorización y el scheduler.
- Nuevos tests: los defaults de `GUNICORN_WORKERS` en `gunicorn.conf.py` y
  `app/extensions.py` quedan fijados entre sí (desincronizarlos volvería cada
  límite 4x más estricto, en silencio), y un test de concurrencia comprueba que
  11 peticiones simultáneas contra un límite de 10/min producen exactamente un
  429.

## [2026.7.13] (2026-08-07)

Release de seguridad. Auditoría STRIDE + OWASP sobre toda la aplicación
(146 ficheros Python, ~116 rutas, 21 blueprints) con remediación completa
y 65 tests de regresión nuevos. Todos los hallazgos son heredados de
wizarrrr/wizarr; ninguno lo introdujo este fork.

### Security

- **Bypass total del segundo factor**: `POST /complete-2fa` autenticaba
  comprobando sólo un valor de sesión que fija el paso de contraseña, así
  que quien conociera la contraseña de un admin con passkey obtenía sesión
  saltándose WebAuthn por completo. Ahora se exige y consume una marca de
  ceremonia verificada.
- **Rate limiting desactivado**: el `Limiter` se construía con
  `enabled=False`, dejando los nueve decoradores `@limiter.limit` como
  no-ops, incluido el de `/login`. Activado, con `scaled_limit` para
  compensar que `memory://` cuenta por worker.
- **Inyección de plantillas (SSTI)** en el filtro `render_jinja`, que
  evaluaba como plantilla el título de los pasos del wizard leído de la
  base de datos: `{{ config }}` filtraba `SECRET_KEY`.
- **CSRF**: no había `CSRFProtect` global, así que 42 de 64 rutas mutantes
  no validaban token, entre ellas `change_password` y `delete_admin`.
- **LDAP**: la rama LDAP omitía el segundo factor, y sin `admin_group_dn`
  configurado provisionaba como administrador a cualquier usuario del
  directorio capaz de hacer bind. Ambas cerradas.
- **Replay de invitaciones**: la invitación se marcaba usada después de
  aprovisionar, de modo que dos envíos simultáneos del mismo código de un
  solo uso creaban dos cuentas. Añadido claim atómico con liberación si el
  aprovisionamiento falla.
- **`DISABLE_BUILTIN_AUTH`** concedía sesión de administrador a cualquiera
  que alcanzase `/login`; ahora exige proxy de confianza e identidad.
- **Dependencias**: 11 CVEs conocidos en 5 paquetes, incluidos 3 en
  `cryptography`, que cifra las credenciales LDAP. Actualizadas.
- Endurecimiento: cookies `Secure`/`HttpOnly`/`SameSite`, `secrets.json`
  en modo `0600`, IP de cliente no falsificable, HMAC de image-proxy de 64
  a 128 bits, `secrets.choice` para los salt, y códigos de invitación
  personalizados con mínimo de 8 caracteres.

### Notas de despliegue

Variables nuevas, todas con valor por defecto seguro:

| Variable | Defecto | Cuándo tocarla |
|---|---|---|
| `RATELIMIT_STORAGE_URI` | `memory://` | Apuntar a Redis para un límite exacto entre workers |
| `TRUSTED_PROXY_COUNT` | `0` | Número de proxies delante; sin esto las cabeceras de IP se ignoran |
| `SSO_TRUSTED_PROXY_IPS` | vacío | Obligatoria si se usa `DISABLE_BUILTIN_AUTH` |
| `SSO_IDENTITY_HEADER` | `X-Forwarded-User` | Cabecera de identidad del proxy SSO |
| `SESSION_COOKIE_SECURE` | `true` | `false` sólo en despliegues LAN sin TLS |

Cambio de comportamiento: con `admin_group_dn` vacío, el login LDAP de
administrador ahora **deniega** en lugar de conceder acceso. El formulario
impide guardar ese estado y el arranque avisa si la base de datos ya lo
tenía.

## [2026.7.12] (2026-07-30)


### ✨ Features

* **users:** the Users page now shows a red "Expired" / orange "Expiring Soon" (≤3 days) / green "Active" badge on every card, and the filter bar gained a matching status dropdown. Both read from the same `get_expiry_status()` helper so the badge and the filter can never disagree, and the filter is applied after `_group_users_for_display()` groups multi-server accounts, against each card's `earliest_expires`.
* **users:** "Recently Expired Users" now scopes to the last 30 days (it previously showed the full unbounded history, hence 690 rows for a handful of accounts — see the fix below) and gained per-row checkboxes with "Delete Selected", plus a "Clear All" button. "All Expired Users" (the full history) gained a "Delete All" button. Both sections read the same `ExpiredUser` table, so deleting from either refreshes both via a shared `refreshExpiredUsers` HTMX trigger.
* **invite:** the Jellyfin "Allow audio playback that requires transcoding" checkbox now defaults to checked for every new invitation, as the code comments already (incorrectly) claimed it did. Video transcoding stays opt-in.


### 🐛 Bug Fixes

* **expiry:** fixed the root cause of the duplicated/stale "Recently Expired Users" history and users that showed "Expires: Never" while actually being inaccessible. `disable_or_delete_user_if_expired()` runs every 15 minutes in production; in `expiry_action="disable"` mode it logged a new `ExpiredUser` row and disabled the account remotely, but never marked `is_disabled` locally or excluded the user from its own query — so the same expired user matched again on every subsequent tick, forever, each time inserting another history row. The query now excludes already-disabled users and the disable branch marks `is_disabled=True` immediately. The manual enable/disable toggle in the admin panel now also persists `is_disabled` locally (it previously only called the remote API). The Jellyfin sync additionally pulls `Policy.IsDisabled` on every poll, so an account disabled directly on Jellyfin (outside Wizarr) self-heals into the local record instead of drifting indefinitely.



### ✨ Features

* **invite:** both password fields on the public create-account form now carry an eye toggle to reveal what was typed, so a typo can be checked without retyping. Open eye = hidden (click to reveal), crossed-out eye = visible (click to hide). The two fields toggle independently and both start hidden — `type="password"` straight from the form, nothing to opt out of. Rendered from one Jinja macro; `type="button"` on the control is load-bearing, since a `<button>` inside a `<form>` defaults to submit and would otherwise post the form on click. Accessible name (`Mostrar contraseña` / `Ocultar contraseña`) and `aria-pressed` swap with the state.


### 🐛 Bug Fixes

* **invite:** the focus animation on form fields now targets the `.form-field` wrapper explicitly instead of `parentElement`. The password inputs sit inside a positioning box for their toggle, so `parentElement` would have animated that box rather than the field. Also skips inputs with no `.form-field` ancestor — `hidden_tag()`'s CSRF input hangs off `<form>` directly, and anime.js throws on a null target.
* **invite:** added a regression guard for the `:root` custom properties in `welcome-jellyfin.html`. djLint's `--format-css`, wired into `.pre-commit-config.yaml`, rewrites the `{{ ... }}` inside that `<style>` block into `{ { ... } }`; Jinja then emits it verbatim and the page silently loses its accent colour while still returning 200. Run djLint on this file with `--lint` only, never `--reformat --format-css`.



## [2026.7.10] (2026-07-27)


### ✨ Features

* **jellyfin:** the Playlists library is never granted to a provisioned account. `_set_specific_folders` filters it out of `EnabledFolders`, matching on `CollectionType == "playlists"` rather than on the display name — library names are admin-chosen and usually localised ("Peliculas", "Documentales"), so a name match would break on exactly the servers that need this. If the Playlists folder was the *only* requested library the user is now restricted to nothing, rather than falling through to `EnableAllFolders` and being granted everything.

  Known limit, stated plainly: this is correct hygiene but not a guarantee. Jellyfin exempts Playlists from the `EnabledFolders` check entirely — `Folder.IsVisible` only consults it when `this is ICollectionFolder && this is not BasePluginFolder`, and `PlaylistsFolder` derives from `BasePluginFolder`. What actually hides the library is `UserViewManager.GetUserViews`, which skips a playlists folder unless the user can see at least one playlist inside it. Fully revoking it is a Jellyfin-side configuration matter, outside Sauron's reach.


### 🐛 Bug Fixes

* **libraries:** scanning libraries no longer resets `Library.enabled`, so unchecking a library in the server settings actually sticks. Both scan paths did this: the "Scan Libraries" button (`media_servers/routes.py`) and the startup scan (`library_scanner.py`), the latter running on **every container boot** — so an admin could uncheck a library, restart, and find it silently re-enabled. New libraries still arrive enabled; only existing rows are left alone. Trade-off: a library that vanished from the server (and was auto-disabled) now stays disabled if it comes back, until the admin re-checks it — under-granting is the safer failure.



## [2026.7.9] (2026-07-27)


### ✨ Features

* **jellyfin:** every Jellyfin account Sauron creates now lands with all of its Home screen sections (User → Settings → Home) set to "None". The layout lives in DisplayPreferences (id `usersettings`, client `emby`) rather than in the user Policy, so this is a separate write on both provisioning paths: invitation redemption (`_do_join`) and the password-prompt route (`/j/<code>/password`). Jellyfin's update handler clears every stored section and re-adds only the keys it receives, so all 10 `homesection*` keys are written — the user-visible count is 7 on current clients and 10 on newer jellyfin-web, and omitting a section would let it fall back to a built-in default. The write is isolated in its own error handler and runs last: the account, its libraries and its policy already exist by then, so a DisplayPreferences failure logs a warning instead of rolling back and orphaning the account. Emby is excluded — `EmbyClient` inherits the method but stores display preferences differently.



## [2026.7.8] (2026-07-25)


### ✨ Features

* **invite:** the public create-account form now states the password rules up front — "Mínimo 8 caracteres, con al menos una mayúscula, una minúscula y un número." — wired to the field with `aria-describedby`. The wording mirrors `JoinForm.password` exactly, including the lowercase requirement, so nothing that reads as valid gets rejected on submit.
* **invite:** the invitation code arrives prefilled from the link and is now `readonly`, so it cannot be edited by accident. `readonly` rather than `disabled` — a disabled input is not submitted and would break redemption. All three render paths for `welcome-jellyfin.html` populate the field before rendering.


### 🐛 Bug Fixes

* **invite:** drop the "Secure invitation system powered by Wizarr" footer from the public invite page, along with the `pageFooter` references in the reveal/back animations that would otherwise hand anime.js a null target on mobile.



## [2026.7.6] (2026-07-22)


### 🐛 Bug Fixes

* **invite:** the "Create account" button is no longer clipped below the fold on the public create-account screen (`welcome-jellyfin.html`) when several validation errors stack up and grow the form past the card height. The card now grows with its content (`h-auto` instead of a fixed `md:h-[520px]`), and when the form renders with errors the page allows vertical scrolling on every breakpoint (drops `lg:overflow-hidden`) with extra padding, so the submit button is always reachable at 100% zoom without resizing the window. Scoped to the server-rendered form-with-errors path; the animated welcome→form flow is unchanged.



## [2026.7.5] (2026-07-22)


### 🌐 Internationalization

* **invite:** the public invite landing and create-account screens (`welcome-jellyfin.html`) are now served in Mexican Spanish (`es_MX`), including every validation/error message — password policy, invalid email, "please correct the highlighted fields", and the "user or e-mail already exists" banner. Scoped to the public invite endpoints only (via the locale selector); the rest of the app keeps its normal locale. Added an `es_MX` catalog, made the form/validator messages translatable with `lazy_gettext`, and set `novalidate` on the form so native browser tooltips route through the translated server-side messages.


## [2026.7.4] (2026-07-22)


### 🚀 Features

* **invite:** validate that the email domain actually resolves in DNS when a user creates their account. Emails on non-existent domains (e.g. `user@1232as.com`) are rejected with "Please enter a valid email address." Checks MX → A → AAAA records and **fails open** on DNS timeouts / unreachable nameservers so a transient DNS issue never blocks a legitimate signup.


## [2026.7.3] (2026-07-22)


### 🚀 Features

* **auth:** protect the admin login page with Cloudflare Turnstile. Configurable from Settings → General → Login Security (site key + secret key), or via `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` / `TURNSTILE_ENABLED` env vars. The `TURNSTILE_ENABLED=false` env override always wins so a bad key can never lock you out. Fails open if Cloudflare's siteverify endpoint is unreachable (missing/invalid tokens are still rejected).


## [2026.7.2] (2026-07-22)


### 🚀 Features

* **notify:** message expiring users who are actively streaming, via an on-screen Jellyfin/Emby session message ("Tu suscripción está por vencer…"). Adds a manual "Notify users who are streaming" button on the Users page and a scheduled job, both idempotent per expiry window.



## [2025.9.1](https://github.com/wizarrrr/wizarr/compare/2025.9.0rc...2025.9.1) (2025-09-05)


### 🚀 Features

* ensure equal height for cards in widget grid on desktop ([b1f8b4f](https://github.com/wizarrrr/wizarr/commit/b1f8b4f8dc302a8176a970653986e1e0dd82f62b))


### 🐛 Bug Fixes

* properly extract next version in PR updates ([13c9555](https://github.com/wizarrrr/wizarr/commit/13c9555c08672d6cdfeab9b149719735529b89fe))

## [2025.9.0](https://github.com/wizarrrr/wizarr/compare/2025.8.5rc...2025.9.0) (2025-09-05)


### 🐛 Bug Fixes

* disable Release-It branch requirement for automated workflow ([8cea746](https://github.com/wizarrrr/wizarr/commit/8cea746f1211012298cda1c9f6b45c4d2ab59e0e))
* display latest invites ([f86964a](https://github.com/wizarrrr/wizarr/commit/f86964a29d3e10d904abc6f148e4de497e6ecca3))
* improve Release-It workflow to properly create PR with changes ([1af32f9](https://github.com/wizarrrr/wizarr/commit/1af32f927f9c3986002acb876491ace25588767e))
* Readme Kwickflix ([4996bce](https://github.com/wizarrrr/wizarr/commit/4996bce2ded3b34e1325a51d85703296d715122d))
