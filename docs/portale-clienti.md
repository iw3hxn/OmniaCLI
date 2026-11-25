# Il portale dei clienti

I clienti (gli utenti dei workspace: admin, developer, user) entrano nella loro parte della macchina dove
girano le loro istanze. Fino al 21/11/2026 niente pubblicava il pannello di OmniaCLI sul web: su una macchina
preparata dal provisioning OmniaCLI ascolta solo su `127.0.0.1:8090`, e l'integratore ci arriva con il link
SSH dell'OmniaCLI centrale. Dai siti Odoo dei workspace passavano solo `/.omniacli/login` (Entra come) e
`/.omniacli/hooks/` (deploy on push). Le prove di accesso del 21/11 usavano un tunnel SSH, che un cliente non
ha, e gli inviti uscivano come `http://127.0.0.1:8090/invite/…`.

## Cosa c'è adesso

1. **Indirizzo del portale**, in *Impostazioni › Opzioni*, riquadro in alto. Solo un amministratore della
   macchina (non l'admin di un workspace) lo vede e lo pubblica.
   - Il nome si controlla come quelli dei workspace: deve essere un nome di host, non un nome di un
     workspace né un nome sotto il loro dominio, e non deve rispondere su un altro sito di questo nginx
     (`nginx -T`).
   - Il DNS viene detto: «punta qui», «punta a X, non a questa macchina», «nessun record».
2. **Pubblica** avvia un job, *Portale dei clienti su `<nome>`*, che chiama l'helper di root
   `workspace-helper portal <nome>`. L'helper:
   - scrive `/etc/nginx/conf.d/omniacli-_portal.conf`;
   - prova la configurazione con `nginx -t`, e se fallisce rimette il file di prima;
   - chiede a Let's Encrypt il certificato `omniacli-_portal`, se il nome porta a questo nginx (la stessa
     sonda `_reaches_here` dei nomi in più dei workspace).

   Finché Let's Encrypt non lo dà, il sito usa un **certificato autofirmato** della macchina
   (`/etc/omniacli/certs/_portal/`, 700/600, 90 giorni). Il job finisce «riuscito con avvisi» e dice perché.
   Solo dopo il job l'indirizzo viene salvato in `options.yaml` (`portal.address`).
3. **Togli…** chiede conferma nel dialogo della pagina e poi rimuove il sito, il certificato di Let's Encrypt
   e quello autofirmato.
4. Il nome `_portal` non può essere il nome di un workspace (`WORKSPACE_NAME` non ammette `_`). Per questo il
   sito e i certificati del portale non si scontrano mai con quelli di un workspace
   (`omniacli-<workspace>.conf`). Un workspace non può prendere il nome del portale: per lui è «un altro sito
   di nginx».
5. **Mai su una macchina il cui nginx non è di OmniaCLI.** Sono le macchine «con Odoo», dove OmniaCLI è stato
   messo accanto a un Odoo già presente, senza `workspaces.storage`. Lì il riquadro mostra il motivo e
   l'helper rifiuta («L'nginx di questa macchina lo tiene chi la amministra…»), come fa il dialogo Indirizzi.
   `/api/info` restituisce `portal: null`.

### Il sito

- **Porta 80**: risponde alle sfide ACME (`/.well-known/acme-challenge/`) e manda tutto il resto in https con
  un 301.
- **Porta 443**: passa *tutto* il pannello all'indirizzo di OmniaCLI (`omniacli_address()`, da
  `/etc/default/omniacli`).
- **Websocket** (console, terminale, log dal vivo): `Upgrade` e `Connection` tramite una `map`,
  `proxy_read_timeout 3600s`, `proxy_buffering off` per le righe dei job e dei log.
- **HSTS**: `max-age=31536000`, uno solo, quello di nginx (`proxy_hide_header` + `add_header … always`), anche
  sulle pagine d'errore di nginx.
- **Intestazioni**:
  - `Host` e `X-Forwarded-Host` sono `$host`;
  - `X-Forwarded-For` e `X-Real-IP` sono `$remote_addr`: nginx è il bordo, e un `X-Forwarded-For` mandato dal
    client viene scartato;
  - `X-Forwarded-Proto` è `https`.
- **Corpo**: `client_max_body_size 50g`, come `uploads.MAX_SIZE` (i backup caricati dal browser o da un
  client MCP), con `proxy_request_buffering off`: nginx non mette il file su disco prima di passarlo.
- **`limit_req` sul modulo di login e su quello degli inviti**: solo POST (la chiave di una GET è vuota e non
  conta), 10 al minuto per indirizzo con raffica 5, poi 429. Dopo c'è il limite di OmniaCLI per indirizzo e
  nome.

## Un nome per workspace (`pannello.acme.it`): no, per ora

Abbiamo deciso di non farlo. I motivi:

- **Il login è uno solo.** Su `pannello.acme.it` entrerebbe anche l'utente di un altro cliente: l'indirizzo
  «di acme» darebbe un'impressione di separazione che non c'è. La separazione vera è quella del pannello: ogni
  utente vede solo i suoi workspace, e resta così.
- **Ogni nome vuole il suo record DNS**, nel DNS del cliente, fuori dal nostro controllo, e un nome nel
  certificato. Un record che manca o punta altrove si porterebbe dietro un certificato da rifare e avvisi a
  ogni pubblicazione.
- **Il nome occupa il posto dei sottodomini del workspace.** `pannello` potrebbe essere il nome di
  un'istanza o di un sottodominio che il cliente ha già.
- **I cookie di sessione sono per nome di host.** Due indirizzi vogliono dire due login per chi passa
  dall'uno all'altro.

Se servirà, l'estensione costa poco. L'helper scrive già un `server_name`, che diventerebbe una lista, e
`address_taken` controlla già i nomi dei workspace. Mancherebbe solo il campo «etichetta» del workspace, con
le sue regole.

## Due nomi sul centrale (AD-018 fetta 3, 25/11/2026)

Il centrale risponde su due nomi suoi: il portale dei clienti, uno solo per tutti, e il **pannello del
personale**. Si pubblicano insieme da *Opzioni › Indirizzo del portale* (`workspace-helper portal
<portale>,<pannello>`): un solo sito, `server_name` con i due nomi, un certificato per tutti e due.

- Sul portale il personale è rifiutato dopo la password giusta, come prima; sul pannello i clienti (anche le
  persone servite per una macchina collegata): 403, e nel registro un `failed` con `status: 403`.
- *Dominio delle passkey*: l'RP ID delle passkey fatte sui due nomi, un dominio sotto cui stanno tutti e due.
  Solo le origini dei due nomi sono accettate: un altro nome dello stesso dominio non può usare un'asserzione.
- Gli indirizzi che OmniaCLI dà ai clienti restano quelli del portale anche quando la pagina è aperta sul pannello.
- Con OmniaCLI fermo, nginx risponde con una pagina statica (503): «Il portale è momentaneamente fermo. I vostri siti
  Odoo funzionano normalmente.», e la stessa frase in inglese.

Dettagli e prove: README, *The two names of the central panel*, e AD-018, «Com'è nel codice (fetta 3)».

## I link che OmniaCLI dà a una persona

- **Inviti** (`invite_url`):
  - quando il portale ha un indirizzo, il link di un utente dei workspace è `https://<portale>/invite/…`;
  - un utente della macchina riceve il portale solo se le Opzioni lo fanno entrare lì (vedi sotto); se no
    riceve l'indirizzo da cui l'ha chiesto, come prima;
  - attraverso un link vince il portale della macchina collegata: lei conosce la sua impostazione, risponde
    con `portal: true`, e l'OmniaCLI centrale tiene il suo link invece di rifarlo con il proprio
    `X-OmniaCLI-Address`. Un OmniaCLI collegato di prima non dice `portal`, e resta tutto come prima.
- **Indirizzo MCP del workspace**, **deploy on push** senza nome dell'istanza e **pagina Aiuto**: usano il
  portale quando c'è (`public_base`).
- L'URL di upload di `upload_backup` resta quello da cui il client MCP ha chiamato: è già il suo.
- **Dove entrano i clienti**:
  - le Opzioni e la pagina Utenti lo dicono: «I clienti entrano da https://…», oppure che il portale non ha
    ancora un indirizzo e cosa fare;
  - la scheda della macchina in *Macchine* lo legge da `/api/info` attraverso il link.

## Revisione di sicurezza: cosa espone e cosa abbiamo scelto

Con il portale, le pagine del pannello sono su internet, comprese quelle degli amministratori della
macchina. Queste sono le scelte, una per punto.

1. **Gli utenti della macchina restano fuori dal portale (default).** Nuova opzione *Utenti della macchina ›
   entrano anche dall'indirizzo del portale*, spenta.
   - Sull'indirizzo del portale (`Host` uguale al nome del portale) entrano solo gli utenti dei workspace.
   - Admin e developer della macchina entrano dal loro link o tunnel SSH, come oggi.
   - Il rifiuto arriva solo dopo una password giusta (403, «Questo indirizzo è il portale dei clienti…») e va
     nel registro accessi come `failed`. Con una password sbagliata la risposta resta il solito 401, e chi
     prova da fuori non scopre quali nomi sono della macchina.
   - Una sessione di un admin aperta altrove non apre il portale: anche `identify` la rifiuta.
   - Token e link (Bearer) non cambiano: un client MCP può usare l'indirizzo pubblico, e va bene così.
2. **Il login** era già limitato per indirizzo e nome (`LoginLimiter`), e ogni tentativo fallito va nel
   registro. Ora ci sono due cose in più:
   - l'indirizzo è quello vero: prima, dietro un nginx, tutti i clienti sarebbero stati `127.0.0.1` e
     avrebbero diviso lo stesso limite;
   - nginx mette il suo `limit_req` davanti.
3. **Di chi ci si fida per l'indirizzo del client.** uvicorn legge `X-Forwarded-For` e `-Proto` solo da
   `127.0.0.1` e `::1` (`cli.TRUSTED_PROXIES`, passato a `forwarded_allow_ips`): una variabile
   `FORWARDED_ALLOW_IPS` nell'ambiente non può allargarlo. Ci arrivano anche il link SSH e il tunnel, ma
   sono autenticati con il loro token. Il registro accessi, il limite del login, gli inviti e i websocket
   leggono `request.client`, che viene da qui.
4. **Il cookie di sessione** è `HttpOnly` e `SameSite=Strict`, e `Secure` quando lo schema è https: dietro il
   portale lo è, perché nginx manda `X-Forwarded-Proto: https`. Il cookie della lingua è `Secure` allo stesso
   modo. Il cookie vale per nome di host: la sessione del portale non è quella del tunnel.
5. **Il controllo di stessa origine** (`_same_origin`) confronta `Origin` con `Host`, e nginx passa `$host`.
   Un modulo mandato da un altro sito resta rifiutato anche dietro il proxy: è provato nel test e su lab1
   (403).
6. **Documentazione API**: `/docs`, `/redoc` e `openapi.json` sono già spenti (`docs_url=None`,
   `openapi_url=None`). Sul portale non c'è niente da nascondere in più.
7. **Cosa resta esposto e va saputo.** Tutto il resto lo decide il ruolo dell'utente, come prima:
   `WORKSPACE_ROUTES` chiude ogni route non elencata agli utenti dei workspace. Sul portale rispondono anche
   tre percorsi senza sessione, tutti con il loro controllo:
   - `/.omniacli/login`, con un ticket monouso legato al nome dell'istanza;
   - `/hooks/deploy/…`, con il segreto del repository;
   - `/invite/<token>`, con token lunghi, monouso, 72 ore, e il `limit_req` davanti.
8. **Il secondo fattore** c'è: vedi la sezione seguente. Con il portale pubblicato, la password di un cliente
   da sola non basta più per entrare da internet.

## Verifica in due passaggi

Il riesame del portale diceva: «non c'è un secondo fattore: la password di un cliente basta per entrare da
internet». NIS 2 (Direttiva 2022/2555, art. 21(2)(j); d.lgs. 138/2024) chiede l'autenticazione a più fattori
dove è appropriata. Dettagli nel README, *Two-step verification*.

- **Cos'è**: dopo la password, il codice di un'app di autenticazione (TOTP, RFC 6238: 30 secondi, 6 cifre,
  SHA-1) o uno dei 10 codici di recupero, ciascuno valido una volta. La sessione nasce solo dopo il secondo
  passaggio.
- **Attivarla**: Account › *Verifica in due passaggi* › *Attiva…*: QR code disegnato da OmniaCLI (libreria
  `segno`, nessun servizio in rete) e lo stesso segreto in chiaro; è attiva dopo un codice giusto. I codici di
  recupero si vedono una volta sola, in una finestra loro. Si disattiva con un codice.
- **Chi deve usarla** (Opzioni › Portale dei clienti › *Verifica in due passaggi obbligatoria*):
  - *utenti all'indirizzo del portale* (default): chi entra dall'indirizzo del portale, e gli utenti dei
    workspace da qualunque indirizzo, appena il portale ha un indirizzo. Altrimenti un cliente la
    salterebbe entrando dall'indirizzo della macchina;
  - *tutti*;
  - *nessuno*.

  Il default: la password rubata si usa da internet, non dalla LAN né dai link, che hanno il loro token. E
  su una macchina aggiornata senza portale non cambia niente. Chi deve e non l'ha ancora attivata la attiva
  subito dopo la password, prima di qualunque pagina (anche dopo un invito).
- **Codici sbagliati**: contano nel limite dei login falliti (stesse attese) e vanno nel registro come
  `failed`; dopo 5 si torna alla password.
- **Chi ce l'ha**: *Utenti* della macchina e la scheda *Utenti* del workspace mostrano «verifica in due
  passaggi attiva»; *Azzera verifica…* la spegne (telefono perso) e l'utente la riattiva. L'admin di un
  workspace lo fa solo per gli utenti del suo workspace soltanto.
- **Token e link** (Bearer) non cambiano.
- **Registro accessi**: evento `second-factor` per attivazione, disattivazione, codici di recupero usati,
  azzeramenti (da chi) e cambio della regola.

**Provato su lab1 (21/11/2026)**, Playwright da questo PC su `https://portale.172-30-0-11.sslip.io`, in italiano,
come `acme-portale` (regola di default), con i codici calcolati da `totp.py` dal segreto mostrato:

1. dopo la password, la pagina di attivazione con il QR code e il segreto; un codice sbagliato dice «Codice
   sbagliato.», quello giusto attiva e mostra i 10 codici di recupero in una finestra loro, poi la Dashboard;
   Account dice «attiva dal 21/11/2026 19:28… Codici di recupero rimasti: 10» e che non si può disattivare;
2. uscita, password e codice dell'app: dentro;
3. uscita, password e un codice di recupero: dentro, ne restano 9;
4. uscita, password e un codice sbagliato: «Codice sbagliato.», e lo stesso codice di recupero una seconda
   volta: rifiutato; senza secondo passaggio `/api/servers` rimanda al login;
5. registro accessi con l'IP vero del PC (`172.30.0.1`) e `workspaces: ['acme']`: `second-factor` «switched on
   at the login», `login` «login form, second step: code» e «…recovery code», `second-factor` «recovery code
   used to log in: 9 left», tre `failed` «second step: wrong code»; `verify-access-log`: catena intatta.
   `second-factor.json` è `600`, dell'utente `omniacli`.

Non provato su lab1: l'azzeramento da un amministratore e l'attivazione da Account (sono nei test,
`tests/test_two_step.py`), e un'app di autenticazione vera su un telefono.

## Provato su lab1 (21/11/2026)

lab1 è la macchina di prova: container privilegiato, Ubuntu 24.04, OmniaCLI 2026.1.5459 con i file di questo
ramo copiati sopra. Gli originali sono in `/root/omniacli-before-portal/`: per tornare indietro si ricopiano
e si riavvia `omniacli`, oppure si usa *Togli…*. Il portale è `portale.172-30-0-11.sslip.io`, che risolve a
lab1.

1. **Pubblicazione** dall'API come admin della macchina, sul suo indirizzo `127.0.0.1:8090`: il job è
   riuscito con avvisi.
   - Il sito è scritto e `nginx -t` passa.
   - Let's Encrypt non raggiunge un indirizzo privato, e il job lo dice. Il certificato è quello autofirmato.
   - L'indirizzo è salvato in `options.yaml`.
2. **http → https**: 301. Su https il login risponde 200, con un solo `Strict-Transport-Security`.
3. **Playwright da questo PC** (Chromium headless, certificato accettato):
   - `acme-admin` entra su `https://portale…`, con un cookie `Secure`, `HttpOnly` e `SameSite=Strict`;
   - vede solo le istanze `acme-*` e niente di `beta`; `/users` gli risponde 403.
4. **Invito**: l'admin della macchina, dal suo indirizzo locale come attraverso il link, invita
   `acme-portale` nel workspace acme.
   - Il link è `https://portale.172-30-0-11.sslip.io/invite/…`.
   - Playwright lo apre, sceglie la password, entra e vede solo acme.
5. **Registro accessi**:
   - il login di `acme-admin`, l'invito accettato e il login di `acme-portale` portano `ip: 172.30.0.1`, cioè
     il PC, non `127.0.0.1`;
   - l'admin della macchina rifiutato sul portale è un `failed` con «at the customers' portal».
6. **Websocket attraverso nginx**:
   - il terminale del cliente arriva a OmniaCLI con l'upgrade (uvicorn risponde 403 perché il terminale non è
     aperto agli utenti dei workspace);
   - con l'opzione dei membri dello staff accesa per un momento, il terminale dell'admin si apre: 101, con il
     prompt del container. Poi l'opzione è tornata spenta.
7. **`limit_req`**: dopo la raffica, i POST di login dallo stesso indirizzo hanno avuto 429 da nginx, mentre
   la GET della pagina restava 200.

**Non provato:**

- il certificato vero di Let's Encrypt, perché lab1 ha un indirizzo privato: la strada è la stessa dei nomi
  in più dei workspace (`_certbot`, `_reaches_here`), già usata in produzione;
- IPv6;
- l'invito creato dall'OmniaCLI centrale attraverso il link SSH verso lab1: è provato nei test
  (`test_through_a_link_the_portal_of_the_linked_machine_wins`), non su lab1.
