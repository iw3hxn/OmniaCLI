# Registro degli accessi per la NIS 2

Requisito: «tutte le azioni degli utenti per la NIS 2 devono essere loggate».
Questa pagina dice cosa registra OmniaCLI, dove, per quanto tempo, come si
dimostra che non è stato alterato e chi lo legge.

> **Il periodo di conservazione.** Letto il 21/11/2026 nell'allegato 1 della determinazione ACN n. 164179 del
> 14/04/2025 (misure di sicurezza di base, soggetti importanti), misura **PR.PS-04**:
> punto 2, «sono conservati in modo sicuro, e possibilmente centralizzato, almeno i log necessari ai fini del
> monitoraggio degli eventi di sicurezza», compresi gli accessi da remoto e con privilegi amministrativi (punto 1);
> punto 3, «in accordo agli esiti della valutazione rischio di cui alla misura ID.RA-05, sono definite e documentate
> le tempistiche di conservazione dei log». **La norma non fissa un numero di mesi**: chiede che il periodo sia
> deciso sul rischio e scritto. I 365 giorni predefiniti sono quindi una scelta di OmniaCLI, non un obbligo: chi
> segue la conformità la conferma o la cambia in Impostazioni › Opzioni › Registro degli accessi, e la scrive nella
> propria procedura (punto 4). L'allegato 2 (soggetti essenziali) ha lo stesso testo, letto lo stesso giorno. La
> determinazione n. 379907/2025, che ha sostituito la 164179 dal 15/01/2026, secondo le domande frequenti NIS
> dell'ACN (DSB.1) ha aggiunto alla parte sui log la sola parola «acquisiti» (si conservano i log acquisiti): nessun
> numero di mesi nemmeno lì. Il testo degli allegati della 379907 non è stato letto direttamente, solo le FAQ.
>
> Sulla verifica in due passaggi, stessa fonte, misura **PR.AA-03** punto 2: «per almeno i sistemi informativi e di
> rete rilevanti e in accordo agli esiti della valutazione del rischio […] sono impiegate modalità di
> autenticazione multifattore».

## Cosa si registra

Una voce per riga (JSON) in `access.jsonl`; i tentativi falliti in
`access-failed.jsonl`, limitati per indirizzo, così non possono spingere fuori
le altre voci.

| Evento | Quando |
|---|---|
| `login`, `logout`, `failed` | login e logout dalla pagina di accesso; primo uso di un link o di un token MCP dopo 30 minuti; password o token sbagliati |
| `action` | ogni richiesta che cambia qualcosa (POST, PUT, DELETE), di chiunque abbia superato il login: una rotta aggiunta in seguito è registrata da sola |
| `read` | ogni lettura di dati dei clienti o di sicurezza (elenco sotto); la stessa pagina della stessa persona dallo stesso indirizzo una volta ogni 10 minuti (un log seguito chiede righe ogni secondo) |
| `mcp` | ogni chiamata MCP, con metodo, tool, server e workspace |
| `shell`, `tunnel`, `download`, `login-as` | apertura e chiusura di shell, psql, Claude, terminali; tunnel; download di backup; «Accedi come» |
| `invitation` | un invito creato, inviato di nuovo o revocato (da chi), e usato dalla persona invitata, che sceglie la sua password (poi il suo `login`); una password mandata con un link scaduto, usato o revocato è un `failed` |
| `second-factor` | verifica in due passaggi attivata o disattivata (da Account o al login, dove è obbligatoria), codice di recupero usato (con quanti ne restano), azzerata da un amministratore (chi), regola «obbligatoria per» cambiata; un codice sbagliato è un `failed` («second step: wrong code») e il `login` dice come è passato («login form, second step: code» o «recovery code») |
| `oauth` | OAuth degli assistenti (claude.ai, ChatGPT…): un client registrato, un consenso dato (livello, giorni, server) o rifiutato dalla persona, un token emesso, rinnovato, revocato (da chi, o perché un refresh token o un codice è stato usato due volte); una richiesta rifiutata (codice, verifier, refresh token o client sbagliati) è un `failed` |
| `passkey` | una passkey registrata (nome, indirizzo) o rimossa dalla persona; il `login` con una passkey dice quale («passkey «Portatile», without password», o «login form, second step: passkey «Telefono»»); una passkey rifiutata (sconosciuta, firma, origine, contatore) è un `failed` |
| `session` | una sessione terminata dalla persona (Account) o da un amministratore (Users), con chi l'ha terminata e quale (browser, sistema, indirizzo, inizio); la sua richiesta seguente è rifiutata |
| `sso` | il single sign-on di un workspace impostato, cambiato o tolto (da chi: provider, issuer, domini, ruolo, se la sua MFA vale come secondo passaggio; mai il client secret), un utente creato dal primo accesso; il `login` dice quale provider («single sign-on Acme ID»); un accesso rifiutato (dominio, e-mail non verificata, firma o nonce dell'ID token, state di un altro browser, utente della macchina con quel nome) è un `failed` |
| `job` | la fine di ogni job, con il suo esito (`success`, `failed`, `stopped`, `interrupted`) e l'errore |
| `siem` | un invio al SIEM che fallisce, e quando riprende |

Campi di ogni voce (quelli vuoti non sono scritti):

- `time` (ora locale con fuso), `utc`, `ns`;
- chi: `kind` (`user`, `token`, `link`, `mcp`, `job`), `name`, `on_behalf`
  (chi agisce tramite un link), `role` (il ruolo in vigore, quello nel
  workspace per i suoi utenti), `workspace` (quello del bersaglio) o
  `workspaces` (quelli dell'utente, per i login dei suoi utenti e dei suoi
  token);
- `ip`;
- l'azione: `method`, `route` (il modello della rotta, es.
  `/servers/{server}/{role}/restart`), `server`, `env` (production/test),
  `detail` (come prima, senza password né token);
- l'esito: `status` (codice HTTP; per un `job`, il suo esito), `job` (gli id dei
  job avviati: la voce `job` con lo stesso id ne dice la fine);
- `prev`: l'hash della voce prima (sotto).

Le letture registrate stanno in un solo posto, `omniacli/audit.py`
(`SENSITIVE_READS`): log delle istanze, statistiche del database, utenti del
database («Accedi come»), posta trattenuta delle staging (elenco e messaggio),
elenchi dei backup e degli archivi (anche sulle destinazioni), report dei
puller, file di configurazione di Odoo, registro degli accessi stesso
(pagina, verifica, esportazioni), log di OmniaCLI, credenziali git, sessioni di un utente
(indirizzi, browser: Users). Ogni altra
rotta GET è in `NOT_SENSITIVE` o in `AUDITED_BY_ROUTE` (download, MCP: si
registrano da sé). Il test `tests/test_audit.py::test_every_read_is_classified`
fallisce quando una rotta GET nuova non è in nessuna delle liste: chi la
aggiunge decide.

## Dove, e per quanto

Nella cartella dei log (`/var/lib/omniacli/logs` col pacchetto). Oltre 5 MB un
file passa a `.1`, i più vecchi salgono di un numero. Un file oltre il decimo
se ne va solo quando la sua voce più recente è più vecchia del periodo
(`audit.retention_days`, 365 di default; 0 = solo per dimensione, come
prima): **nessuna voce più giovane del periodo è eliminata per spazio**.

La pagina della macchina e quella del registro avvisano quando il disco non
basta: spazio libero minore del doppio di quanto il periodo richiede ancora,
al ritmo dei giorni registrati, o minore di 512 MB. A disco pieno le voci
nuove si perdono (un'azione non viene mai fermata dal registro).

La purga del pacchetto (`apt purge omniacli`) cancella la cartella dei log, e
con essa il registro: esportatelo prima.

## Integrità

Ogni riga porta in `prev` lo SHA-256 della riga prima nello stesso file, o
dell'ultima del file più vecchio quando ne apre uno: una catena per flusso
(`access.jsonl`, `access-failed.jsonl`) che continua attraverso la rotazione.
Le voci scritte prima di questa versione non hanno `prev`: la catena parte
dalla prima che lo ha.

Verifica:

- pagina Registro degli accessi › «Verifica la catena» (`GET /api/access/verify`);
- da shell: `omniacli verify-access-log -c /etc/omniacli/omniacli.yaml` (esce
  con 1 se la catena è rotta, e dice file e riga).

Una riga modificata, tolta o aggiunta in mezzo si vede alla riga dopo. Non si
vedono: l'ultima riga tolta, o un file intero più vecchio tolto (la catena
riparte dal più vecchio rimasto). Contro chi ha root sulla macchina l'unica
ancora vera è una copia fuori: per questo c'è il SIEM.

## Orologio

La pagina del registro mostra se l'orologio è sincronizzato (`timedatectl`,
altrimenti `chronyc tracking`); la pagina della macchina avvisa quando non lo
è. Ogni voce ha l'ora UTC e quella locale.

## Chi lo legge

- **Amministratore della macchina**: tutto, in Impostazioni › Utenti › Registro
  degli accessi, filtrabile per periodo, utente, evento e testo, esportabile in
  CSV e JSON.
- **Amministratore di un workspace**: nella pagina del workspace › Registro
  degli accessi (`/workspaces/<ws>/audit`), le sole voci del suo workspace:
  quelle con `workspace` uguale al suo (azioni, letture e job sulle sue
  istanze, di chiunque, anche degli amministratori dell'integratore) e i
  login, logout e tentativi falliti dei suoi utenti e token. Stessi filtri ed
  esportazione CSV/JSON di un periodo, per i suoi auditor.
- Un cliente non vede mai le voci di altri workspace né quelle della macchina
  (utenti, link, opzioni, voci senza workspace). Utenti e sviluppatori di un
  workspace non lo vedono.
- La lettura del registro e ogni esportazione sono a loro volta registrate.

## SIEM

Impostazioni › Opzioni › Registro degli accessi: server syslog, protocollo
(UDP, TCP, TCP+TLS con i certificati di sistema o un file di CA), porta.
Ogni voce, appena scritta, è inviata in RFC 5424 (facility authpriv; TCP e TLS
con l'octet counting di RFC 5425), il messaggio è la voce JSON. L'invio sta in
un thread suo con una coda: un SIEM che non risponde non ferma mai un'azione.
Le voci aspettano in memoria (al massimo 10.000, poi si perdono le più vecchie,
contate) e si riprova con pause crescenti fino a 60 s; il guasto è scritto nel
registro una volta (`siem`), e così la ripresa, con le voci perse. Un riavvio di
OmniaCLI con il SIEM giù perde le voci in coda (restano nel file locale).

## Limiti noti

- Il periodo di 365 giorni è una scelta, non un obbligo di legge: va confermato e documentato da chi segue la
  conformità (vedi sopra, PR.PS-04 punto 3). Per le installazioni di questo fork Carlo l'ha confermato il 22/11/2026.
- I job di una chiamata MCP non sono legati per id alla voce `mcp`: la voce
  `job` ne dice l'autore (`mcp: <token>`), l'ora e l'esito.
- Un job interrotto da un riavvio di OmniaCLI non ha la sua voce `job`: il job
  stesso dice «interrupted».
- L'appartenenza a un workspace di una voce di login è quella del momento del
  login.
