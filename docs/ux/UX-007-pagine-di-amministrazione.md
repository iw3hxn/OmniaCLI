# UX-007 — Le pagine di amministrazione: Collegamenti, Utenti, Gruppi, Workspace

Stato: contratto di design, prima della build. Implementa chi scrive `omniacli/templates/links.html`, `users.html`,
`groups.html`, `workspace.html`, `workspaces.html` e `_workspaces_list.html` (Jinja + JS senza framework, classi
esistenti `.tabs`, `.panel`, `.primary`, `.small`, `details`, `dialog`). Le stringhe vanno in `omniacli/i18n/it/*.json`.
Nessuna route nuova: le tre che aiuterebbero sono in §8, fuori da questo contratto.
Valgono le regole comuni e i criteri di UX-001…006: questo contratto li applica a queste pagine e non li ripete.
Origine: Carlo, 20/11/2026: «OmniaCLI la vedo ancora troppo poco maturo. Gli manca proprio l'usabilità». Dopo la
pagina dell'istanza (UX-006) tocca alle pagine di amministrazione. Misurato con Playwright, 1440×900, IT, admin
della macchina:

| Pagina | Altezza | Pulsanti senza `title` | Campi in vista | `.primary` visibili |
|---|---|---|---|---|
| `/links` | 3709 px | 23 su 32 | 29 | 1 |
| `/users` | 1238 px | 7 | 11 | 1 |
| `/groups` | 900 px | 11 | — | 2 (Salva in alto e in basso) |
| `/workspaces/<nome>` | 2883 px | 24 (con i `summary`) | ~20 | 4 (Nuova istanza, Crea, Nuovo token, Salva credenziali) |
| `/workspaces` | 1224 px | 4 | 1 | 1 |

## 0. Cosa vede oggi l'utente (aggregato del 20/11)

| Pagina | Cosa c'è | Difetto |
|---|---|---|
| Collegamenti | «Altro»; pannello della chiave SSH sempre aperto; tabella con, per ogni collegamento, URL, token, file CA in campi aperti; sotto ogni collegamento SSH la riga aperta con server, porta, utente, impronta, la riga per `authorized_keys` e il blocco `sshd_config` | con 4 collegamenti la pagina è alta 3709 px; la cosa da sapere («risponde?») non c'è: compare solo dopo «Prova il collegamento»; 8 blocchi di configurazione uguali ripetuti; «SSH…», «URL», «File CA», «Token» come etichette; la riga «nuovo collegamento» in fondo, sotto la piega |
| Utenti | tre pannelli in colonna (Utenti, Collegamenti in entrata, Token MCP), ognuno con ricerca, tabella e riga d'aggiunta | 11 campi aperti; ogni riga ha un menu del ruolo, un campo password e «Salva», che sembra salvare anche senza modifiche; percorso del file `users.yaml` in faccia; «Token MCP», «/mcp» come etichette; il banner del token nuovo ha un secondo primario «Fatto» |
| Gruppi | due «Salva» primari (testa e piede); per lista «Aggiungi gruppo» e le etichette dei membri noti | due primari; «Salva» attivo senza modifiche; «Server noti adesso: ancora nessuno» mentre la macchina ha `odoo19-odoo` (vedi Non verificato) |
| Workspace `<nome>` | Disco; Istanze (con repository, «Aggiungi repository», parametri di Odoo per ruolo, Rimuovi); Nuova istanza (modulo aperto + «Da un archivio completo»); Nomi e certificato; Destinazioni dei backup; Avvisi; Token MCP; Credenziali git; Utenti: nove sezioni in colonna | 2883 px; 4 primari; repository e parametri di Odoo **ripetono** la pagina dell'istanza (UX-006 › Impostazioni); «Rimuovi» istanza con un `confirm()` semplice, mentre la pagina dell'istanza chiede di scrivere il nome; il modulo «Nuova istanza» è sempre aperto; i ruoli dei token in inglese («developer»); `alert()` dopo il caricamento del certificato |
| Workspace (elenco) | Questa macchina; ricerca; elenco; «Altro»; «Noti adesso» (server e utenti); Template | il clic su una riga apre `?edit=` (il modulo della macchina), non la pagina del workspace: **due pagine per lo stesso workspace**, ognuna con un suo «Utenti» (una textarea `nome ruolo` e una tabella); «Noti adesso» è un aiuto per chi configura, non un dato del lavoro |

## 1. Persone e lavoro primario

| Persona | Chi è nel codice | Pagine |
|---|---|---|
| **Integratore, admin della macchina** (Carlo) | `me.can("admin")` e nessun workspace (`machine_admin`) | tutte |
| **Admin di un workspace** (raramente un collega o il cliente) | `scoped`, ruolo `admin` nel workspace | solo `/workspaces/<suo>`: `in_workspaces` rifiuta `/links`, `/users`, `/groups` e `/workspaces` |
| developer, user | — | nessuna: tutte le route sono `Depends(admin)` |

Device: desktop, 1440×900. Si usano poche volte al mese: l'aiuto al passaggio del mouse conta più della densità.

| Pagina | Lavoro primario (l'unica domanda) | Primario |
|---|---|---|
| **Collegamenti** | «Le macchine dei clienti rispondono? Ne aggiungo una.» | **Nuovo collegamento** (testa) |
| **Utenti** | «Chi entra qui e con che ruolo? Aggiungo, cambio, tolgo.» | **Nuovo utente** / **Nuovo token di collegamento** / **Nuova chiave per un assistente**, secondo il tab (testa) |
| **Gruppi** | «In che ordine e sotto quali titoli la Dashboard mostra le istanze?» | **Salva** (barra d'azione, attivo solo con modifiche) |
| **Workspace `<nome>`** | «Quali istanze ha questo cliente, a che indirizzo, e chi ci entra?» | **Nuova istanza** (testa) |
| Workspace (elenco) | «Quanto consuma ogni cliente? Apro il suo.» | **Nuovo workspace** (testa) |

## 2. Regole che UX-007 aggiunge a quelle comuni

1. **Una riga = un oggetto, letto; non un modulo aperto.** Nelle tabelle di queste pagine non ci sono campi
   modificabili: si cambia con «Modifica…», che apre un `dialog` con i campi, «Salva» (primario del dialogo) e
   «Annulla». Fanno eccezione i campi di ricerca e la tabella dei sottodomini (§4.4 B), dove la riga *è* il dato.
2. **Un salvataggio è disattivato finché non cambi qualcosa** (regola 2 di UX-006), anche nei dialoghi.
3. **Un segreto si mostra una volta, in un `dialog`**, con «Copia» e un solo pulsante di chiusura «Ho copiato il
   token» (primario del dialogo). Vale per i token di collegamento, quelli degli assistenti AI e quelli del workspace.
   Al posto del banner con «Fatto» e del passaggio per `sessionStorage`.
4. **Il percorso dei file sul disco non è un'etichetta.** `users.yaml`, il file dei collegamenti e quello dei gruppi
   vanno in un `details` «Dove è salvato ▸» in fondo alla pagina, in `.small mono`.
5. **Ogni `pre.copyable` ha il suo «Copia» con `title`** «Copia negli appunti» (in `base.html` riga 583, una volta per
   tutte le pagine).
6. **Nessun `data.error` grezzo**: `refuse()` restituisce l'inglese, il JS lo passa da `tr` (tabella in §5).
7. **Nessun `alert()`**: l'esito va in una riga sotto il pulsante o nella fascia del job.

## 3. Visibilità

| Elemento | Admin della macchina | Admin del workspace | Rete di sicurezza |
|---|---|---|---|
| Collegamenti, Utenti, Gruppi, elenco dei Workspace | sì | — (403, come oggi) | il menu Impostazioni non mostra le voci che danno 403 |
| Workspace › Istanze, Nomi sul web, Utenti, Servizi esterni | sì | sì | — |
| Workspace › **Limiti e disco** (D1) | sì | — | se il workspace non ha disco o dominio, Istanze e Nomi dicono «lo imposta l'amministratore della macchina» e, per lui, linkano qui |
| Nuova istanza › «Copia da un collegamento» | sì | — (`links` vuoto, come oggi) | — |

## 4. Pagina per pagina

### 4.1 Collegamenti (`/links`)

**Sopra la piega, in ordine**
1. Testa: «Collegamenti», una riga di descrizione («Altre istanze di OmniaCLI che gestisci da qui: le loro istanze
   compaiono nella Dashboard.») e il primario **Nuovo collegamento**.
2. Ricerca (solo con più di 8 collegamenti).
3. **Tabella, una riga per collegamento** (alta ~56 px):

| Colonna | Contenuto |
|---|---|
| Collegamento | il nome, link alle sue pagine (`/links/<nome>/`) |
| Come si raggiunge | «Diretto · https://…» oppure «Tramite SSH · odoo.ber.cat:22528»; l'indirizzo in `.small mono` |
| Stato | pallino e parola: **Risponde** (verde) «OmniaCLI 2026.1.5444 · tu sei amministratore» · **Non risponde** (rosso) con la causa in una riga (§5) · **Tunnel in avvio** (ambra) · **Da provare** (grigio, prima della risposta). Lo riempie il JS all'apertura chiamando `/api/links/<nome>/test`, in parallelo (D2) |
| Azioni | Prova · Modifica… · Elimina… |

   Avviso in linea, rosso, **fuori** dal dialogo e sotto «Come si raggiunge»: «Il token viaggia in chiaro: usa questo
   collegamento solo dentro una VPN o via SSH.» (oggi c'è; con `http://` senza SSH).
4. «Dove è salvato ▸» e «Come funziona ▸» chiusi, in fondo (il testo dell'«Altro» di oggi).

**Dialogo «Nuovo collegamento» / «Modifica <nome>»**, in quest'ordine:
1. Nome (solo nel nuovo; vedi «Rinomina» sotto); Indirizzo; Token («invariato» nel Modifica, «il token dalla sua
   pagina Utenti › Altre istanze» nel nuovo).
2. Interruttore **«Passa da un server SSH del cliente»**, spento di default (acceso se il collegamento ha già l'SSH).
   Da acceso: Server, Porta, Utente, «Chiave del server» con l'impronta e «Leggi la chiave del server», e sotto il
   `details` **«Cosa mettere sul server del cliente ▸»**, chiuso, con la riga per `authorized_keys`, il blocco
   `sshd_config` e i comandi come oggi. Se la chiave di OmniaCLI non c'è ancora, al suo posto la riga «Prima serve
   la chiave SSH di questo OmniaCLI.» e il secondario **Crea la chiave**.
3. `details` **«Avanzate ▸»**, chiuso: Certificato dell'autorità (oggi «File CA», vuoto = quelli di sistema).
   Rete di sicurezza: aperto se il campo ha un valore.
4. Salva (primario del dialogo, disattivato finché non cambi) · Annulla.

**Lista HIDE**

| Elemento | Dove va | Rete di sicurezza |
|---|---|---|
| Pannello «La chiave SSH di questo OmniaCLI» | nel dialogo, sotto «Cosa mettere sul server del cliente ▸» (la chiave pubblica è dentro la riga di `authorized_keys`) | se un collegamento usa l'SSH e la chiave manca, la riga del collegamento è rossa con «Manca la chiave SSH: Modifica…» |
| Campi URL, token, CA in ogni riga | nel dialogo | — |
| Righe SSH aperte (server, porta, utente, impronta, due blocchi da incollare) | nel dialogo, sotto l'interruttore | lo stato del tunnel resta nella colonna Stato |
| Riga «nuovo collegamento» in fondo | dialogo dal primario | stato vuoto (sotto) |
| File dei collegamenti, `/links/<nome>/` | «Dove è salvato ▸», «Come funziona ▸» | — |

**Testi**

| Oggi | Diventa |
|---|---|
| URL | Indirizzo |
| File CA / «CA di sistema» | Certificato dell'autorità / «quelli di sistema» |
| SSH… / Server SSH | interruttore «Passa da un server SSH del cliente» / Server |
| Leggi la chiave dell'host | Leggi la chiave del server |
| Prova il collegamento | Prova |
| Elimina | Elimina… |
| Aggiungi collegamento | Nuovo collegamento |

| Pulsante | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|
| nuovo | **primario** (testa) | Nuovo collegamento | «Aggiunge un'altra istanza di OmniaCLI da gestire da qui: serve il suo indirizzo e un token creato sulla sua pagina Utenti › Altre istanze. Le sue istanze poi compaiono nella Dashboard.» |
| data-test | secondario `.small` | Prova | «Chiede all'altra istanza chi sei lì: risponde con la sua versione e il ruolo che hai. Non cambia niente. Con l'SSH, prima guarda se il tunnel è attivo.» (`/api/links/{n}/test`) |
| modifica | secondario `.small` | Modifica… | «Cambia indirizzo, token, server SSH e certificato del collegamento. Il token lasciato vuoto resta quello salvato.» |
| data-delete | pericolo `.small`, conferma | Elimina… | «Toglie il collegamento da questo OmniaCLI: le sue istanze escono dalla Dashboard e il tunnel SSH si chiude. Sull'altra istanza il token resta valido finché non lo revochi lì. Non controlla le istanze copiate da qui né le pubblicazioni che passano di qui.» (`/api/links/delete`). Conferma: «Eliminare il collegamento <nome>? Le sue istanze escono dalla Dashboard.» |
| data-save (dialogo) | primario del dialogo | Salva | «Salva il collegamento. Se cambi server SSH o indirizzo, il tunnel si chiude e si riapre con i dati nuovi. Disattivato finché non cambi qualcosa.» (`/api/links/save`, `ssh_tunnels.sync`) |
| data-fetch | secondario `.small` | Leggi la chiave del server | «Chiede al server SSH la sua chiave e ne mostra l'impronta: confrontala con quella che il cliente legge sul server (ssh-keygen -lf …). Si tiene al salvataggio, e da lì si accetta solo quella.» (`/api/links/ssh/host-key`) |
| make-key | secondario `.small` | Crea la chiave | «Crea la chiave SSH con cui questo OmniaCLI entra nei server dei clienti: una per tutti i collegamenti. La riga da mettere sul server la limita all'inoltro di quel collegamento.» (`/api/links/ssh/key`) |
| interruttore SSH | interruttore | Passa da un server SSH del cliente | «Per un cliente che espone solo l'SSH: OmniaCLI tiene aperto un tunnel verso quel server e manda di lì tutto il collegamento, cifrato. Lo riapre se cade.» |
| Annulla (dialogo) | secondario | Annulla | «Chiude senza salvare» |

- **Rinomina** (opzionale, senza backend nuovo): nel dialogo Modifica il campo Nome è modificabile e chiama
  `/api/links/rename`, che sposta anche le istanze copiate da lì e le pubblicazioni. `title`: «Cambia il nome del
  collegamento anche nelle istanze copiate da qui e nelle pubblicazioni. Rifiutato mentre un job lavora su una di
  quelle istanze.»
- **Vuoto**: «Nessun collegamento. Un collegamento porta qui le istanze di un altro OmniaCLI, per esempio quello di un
  cliente: sull'altro crei un token (Utenti › Altre istanze), qui lo incolli con Nuovo collegamento.»
- **Molte righe**: oltre 8 compare la ricerca; l'ordine è alfabetico, con i «Non risponde» in testa.

### 4.2 Utenti (`/users`)

**Tre tab** (stessa classe `.tabs` di UX-006, il tab in `#hash`): **Persone (N) · Altre istanze (N) · Assistenti AI (N)**.
Il primario in testa cambia con il tab, e ce n'è sempre uno solo. Accanto, il secondario «Registro degli accessi».

**Persone**
- Riga: Nome («(tu)» accanto) · Ruolo a parole · Azioni (Modifica… · Elimina…).
- Un utente della configurazione principale: badge «configurazione principale» con `title` «Si cambia nel file di
  configurazione principale della macchina, non da qui», nessuna azione.
- Un utente di ruolo «solo workspace»: in `.small` i workspace dove entra, come link.
- «Cosa può fare ogni ruolo ▸» resta, chiuso, sotto la tabella.
- **Dialogo Nuovo utente**: Nome, Ruolo (default utente), Password («almeno N caratteri»), Crea · Annulla.
  **Dialogo Modifica**: Ruolo (disattivato per te stesso, con il motivo accanto), Nuova password («vuota: resta»),
  Salva · Annulla.

**Altre istanze** (oggi «Collegamenti in entrata»). Una riga dice: «Le altre istanze di OmniaCLI che possono gestire
questa. Il token si incolla lì, nella loro pagina Collegamenti.»
- Riga: Nome · Ruolo massimo · Azioni (Modifica… · Revoca…).
- Dialogo Nuovo: Nome («chi si collega, es. omniasolutions»), Ruolo (default utente), «Crea il token» · Annulla.
  Poi il dialogo del segreto (regola 3).

**Assistenti AI** (oggi «Token MCP»). Una riga dice: «Le chiavi con cui un assistente AI (Claude, Cursor…) legge le
istanze e usa i suoi strumenti.» `MCP` compare solo in `.small mono`.
- Riga: Nome · Ruolo · se è di un workspace, il badge-link «workspace <nome>» · Azioni (Modifica… · Revoca…).
- Dialogo Nuovo: Nome («chi la usa, es. portatile-carlo»), Ruolo (default sviluppatore), Crea · Annulla. Poi il
  dialogo del segreto, che dice anche «Mettila nell'assistente come Authorization: Bearer <chiave>», in `mono`.

**Lista HIDE**

| Elemento | Dove va | Rete di sicurezza |
|---|---|---|
| Menu del ruolo e campo password in ogni riga | dialogo Modifica | il ruolo resta leggibile nella riga |
| «Salvato in /var/lib/omniacli/users.yaml…» | «Dove è salvato ▸» | — |
| Ricerca di ogni tabella | solo oltre 8 righe | — |
| La frase sui ruoli MCP | nel `title` del menu Ruolo del dialogo | — |
| Banner del segreto con «Fatto» | dialogo del segreto | — |

| Pulsante | Tab | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| nuovo utente | Persone | **primario** | Nuovo utente | «Crea un utente che entra in questo OmniaCLI con nome e password, con il ruolo che scegli.» |
| modifica utente | Persone | secondario `.small` | Modifica… | «Cambia il ruolo o la password. Non puoi cambiare il tuo ruolo, né togliere il ruolo amministratore all'ultimo amministratore.» (`/api/access/users/save`) |
| elimina utente | Persone | pericolo `.small`, conferma | Elimina… | «Cancella l'utente: da adesso non entra più. Non puoi eliminare te stesso né l'ultimo amministratore.» (`/api/access/users/delete`). Conferma: «Eliminare <nome>? Non potrà più entrare.» |
| nuovo token di collegamento | Altre istanze | **primario** | Nuovo token di collegamento | «Crea il token con cui un altro OmniaCLI gestisce questo: lo vedi una volta sola e lo incolli nella sua pagina Collegamenti. Chi agisce da lì non supera il ruolo che scegli.» (`/api/access/links/save`) |
| modifica collegamento | Altre istanze | secondario `.small` | Modifica… | «Cambia il ruolo massimo di chi arriva da quell'istanza. Il token resta lo stesso.» |
| revoca collegamento | Altre istanze | pericolo `.small`, conferma | Revoca… | «Annulla il token: quell'istanza non entra più qui, e la sua prova del collegamento dirà "token rifiutato". Per riammetterla serve un token nuovo.» (`/api/access/links/delete`) |
| nuova chiave AI | Assistenti AI | **primario** | Nuova chiave per un assistente | «Crea una chiave per un assistente AI: legge le istanze di questa macchina e usa gli strumenti che il ruolo permette (utente legge soltanto; sviluppatore aggiorna e fa il backup delle istanze di test; amministratore aggiunge anche server in bozza). La vedi una volta sola.» (`/api/access/tokens/save`) |
| modifica chiave AI | Assistenti AI | secondario `.small` | Modifica… | «Cambia il ruolo della chiave; la chiave resta la stessa.» |
| revoca chiave AI | Assistenti AI | pericolo `.small`, conferma | Revoca… | «Annulla la chiave: l'assistente che la usa smette subito di funzionare. Se la chiave è di un workspace, sparisce anche dalla sua pagina.» (`/api/access/tokens/delete`) |
| registro | testa | secondario | Registro degli accessi | «Apre gli ultimi accessi: entrate, tentativi falliti e chiamate degli assistenti, dal più recente.» (`/access`) |
| ho copiato | dialogo del segreto | primario del dialogo | Ho copiato il token | «Chiude: il token non si vedrà più. Se lo perdi, revocalo e creane uno nuovo.» |

- **Vuoto** — Altre istanze: «Nessuna istanza può gestire questa. Se un altro OmniaCLI deve farlo, crea qui il suo
  token.» Assistenti AI: «Nessun assistente AI ha accesso. Crea una chiave per collegarne uno.»

### 4.3 Gruppi (`/groups`)

**Sopra la piega, in ordine**
1. Testa: «Gruppi», la riga «I titoli sotto cui la Dashboard mostra le istanze, in quest'ordine.», nessun pulsante.
2. Due sezioni: **Istanze di questa macchina** e **Istanze collegate** (oggi «Server» e «Collegate»). In ognuna, i
   gruppi; sotto, «+ Aggiungi gruppo» e la riga dei membri noti come etichette cliccabili.
3. **Barra d'azione** in fondo alla finestra (`position: sticky; bottom: 0`): «N modifiche» · **Salva** (primario,
   disattivato senza modifiche) · «Annulla» (link).

- Nome del gruppo e membri restano campi aperti: qui la riga *è* il dato, perché il modulo è la pagina.
- «Come si indicano i membri ▸» resta, chiuso. Il percorso del file va in «Dove è salvato ▸».
- **Annulla con modifiche**: chiede «Lasciare la pagina senza salvare le N modifiche?» (`beforeunload` solo con un
  conteggio maggiore di 0).

| Pulsante | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|
| save | **primario** (barra), attivo con modifiche | Salva | «Salva i gruppi delle due liste e torna alla Dashboard, che li mostra subito. Non tocca le istanze. Disattivato finché non cambi qualcosa.» (`/api/groups/save`) |
| annulla | link | Annulla | «Torna alla Dashboard senza salvare» |
| add-top | secondario | + Aggiungi gruppo | «Aggiunge un gruppo in fondo a questa lista: dagli un nome e i membri, uno per riga» |
| add-sub | secondario `.small` | + Sottogruppo | «Aggiunge un gruppo dentro questo (fino a 4 livelli): un'istanza va nel primo sottogruppo che la nomina» |
| up / down | secondario `.small` | ↑ / ↓ | «Sposta il gruppo sopra» / «Sposta il gruppo sotto» (ci sono già) |
| remove | pericolo `.small` | Togli | «Toglie il gruppo e i suoi sottogruppi dalla lista; le istanze tornano sotto Altri. Vale al salvataggio.» |
| data-member | etichetta | `<nome>` | «Aggiunge <nome> all'ultimo gruppo che hai modificato (se non ce n'è uno, all'ultimo della lista)» |

- **Vuoto**: «Nessun gruppo: la Dashboard mostra le istanze in un solo elenco. Un gruppo le raccoglie sotto un
  titolo, per esempio "Clienti" con `acme-*`.» Membri noti vuoti: «Nessuna istanza ancora.»

### 4.4 Pagina del workspace (`/workspaces/<nome>`)

**Testa**: «Workspace odoo19 · <etichetta>» e la riga del disco, che oggi è un pannello a sé: «Disco: 4,1 GiB usati
su 127 GiB · 2 istanze su 5». Il numero delle istanze compare solo se c'è il limite. Primario **Nuova istanza**,
che apre il dialogo. Lo stesso primario resta in tutti i tab.

**Cinque tab**: **Istanze · Nomi sul web · Utenti · Servizi esterni · Limiti e disco** (l'ultimo solo per l'admin
della macchina, D1).

| Oggi | Dove va | Perché |
|---|---|---|
| Disco (pannello) | una riga nella testa | è il budget del cliente, con il suo confronto (usato su limite) |
| Repository e «Aggiungi repository» in ogni istanza | **via**: la pagina dell'istanza › Impostazioni › Codice | doppione di UX-006 |
| «Parametri Odoo di produzione/test» | **via**: la pagina dell'istanza › Impostazioni › Opzioni di Odoo | doppione di UX-006, dove si salvano con la stessa route `parameters/save` |
| «Rimuovi» istanza | **via**: la pagina dell'istanza, dove chiede di scrivere il nome | qui era protetto solo da un `confirm()` |
| Nuova istanza (modulo aperto) | dialogo dal primario | si usa di rado |
| Nomi e certificato | tab **Nomi sul web** | — |
| Utenti | tab **Utenti** | — |
| Destinazioni dei backup, Avvisi, Token MCP, Credenziali git | tab **Servizi esterni** | è quello con cui il workspace parla fuori: posta, assistenti, host git, archivi remoti |
| `/workspaces?edit=<nome>` (limiti, storage, dominio, server, membri) | tab **Limiti e disco** (D1) | una pagina per workspace |

**A. Istanze** (aperto)
- Tabella. Colonne:
  - Istanza: il nome, link alla sua pagina;
  - Indirizzi: produzione e test, presi da `sites`, come link ↗;
  - in `.small`, «Codice e opzioni ›», link al tab Impostazioni dell'istanza.
- **Vuoto**: «Nessuna istanza ancora. Nuova istanza ne crea una da un template, o da un archivio completo di un'altra
  macchina.» Se non si può creare, il motivo di oggi (`storage_problem`, Docker, limite raggiunto, nessun template)
  sostituisce questo testo, e il primario in testa è disattivato con lo stesso motivo nel `title` dello `span`.

**Dialogo Nuova istanza**
- Scelta in testa: **Da un template** (default) · **Da un archivio completo**.
- Da un template: Nome; Template; i campi del template; la riga «Il database nuovo ha l'utente admin con password
  admin: cambiala al primo accesso.»; Crea (primario del dialogo) · Annulla.
- Da un archivio completo: Nome; Archivio (elenco), «oppure carica un file .zip», e per l'admin della macchina
  «oppure copia da un collegamento»; «Neutralizza» acceso. Poi **Crea dall'archivio** (primario del dialogo) ·
  Annulla.

**B. Nomi sul web**
- Una riga: «Le istanze rispondono sotto odoo.ber.cat. Ogni nome vuole un record DNS che punti a questa macchina.»
- Tabella dei nomi (Istanza · Copia · Indirizzi ↗) e i campi Istanza principale e Certificato.
- Sottodomini come oggi: è una tabella modificabile, eccezione della regola 1.
- «Salva e pubblica» (secondario, attivo con modifiche) e «Pubblica di nuovo» (secondario).
- «Come si formano i nomi ▸» (oggi «Altro») resta chiuso.
- Certificato del cliente: textarea visibili solo con «del cliente», come oggi.
- **Vuoto**: «Il workspace non ha ancora un dominio: lo imposta l'amministratore della macchina.» Per lui il testo è
  un link al tab Limiti e disco.

**C. Utenti**: stesso schema della pagina Utenti (regola 1).
- Riga: Nome · Ruolo · in `.small` «entra anche altrove: la password la cambia l'amministratore della macchina» ·
  Modifica… · Togli….
- «I ruoli ▸» resta chiuso.
- Dialogo Nuovo utente: Nome, Ruolo, Password, Aggiungi · Annulla.
- **Vuoto**: «Nessun utente: solo gli amministratori della macchina entrano in questo workspace. Un utente creato qui
  vede solo le sue istanze.»

**D. Servizi esterni**, quattro sezioni corte in quest'ordine:
1. **Avvisi**: l'elenco a parole («Avvisa mario@acme.it, https://hooks… quando un'istanza non risponde e quando
   torna») e «Modifica…». **Vuoto**: «Nessuno viene avvisato quando un'istanza non risponde.» con «Aggiungi…».
2. **Destinazioni dei backup**: «N destinazioni» e il link «Apri ›».
3. **Credenziali git**: la tabella del macro `_credentials.html` com'è, ma «Salva» diventa secondario.
4. **Assistenti AI** (oggi «Token MCP»): schema del tab Assistenti AI di §4.2. I ruoli passano da `tr()`, non più
   «developer». Il token nuovo si mostra nel dialogo del segreto, non più via `sessionStorage`.

**E. Limiti e disco** (D1; solo admin della macchina)
- Il modulo di `?edit=` di oggi.
- «Server, uno per riga» diventa una lista di caselle sui server noti.
- «Utenti, uno per riga: nome ruolo» **va via**: gli utenti si gestiscono nel tab Utenti.
- «Limiti dei parametri» resta, in «Avanzate ▸».
- Salva (secondario, attivo con modifiche) · Elimina il workspace….

**Lista HIDE**

| Elemento | Dove va | Rete di sicurezza |
|---|---|---|
| Moduli Nuova istanza e Da un archivio completo | dialogo | il motivo per cui non si può creare è sempre a schermo (tab Istanze) |
| Repository e parametri di Odoo per istanza | pagina dell'istanza | link «Codice e opzioni ›» in ogni riga |
| «Rimuovi» istanza | pagina dell'istanza | — |
| Spiegazione dei nomi (`<instance>.dominio`, `<sub>.test.dominio`) | «Come si formano i nomi ▸» | — |
| Avvisi, token, credenziali, destinazioni | tab Servizi esterni | — |
| «Altro» dell'elenco e «Noti adesso» | elenco › «Come funziona ▸» | un server che non è in nessun workspace resta elencato lì |

| Pulsante | Tab | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| nuova istanza | testa | **primario** | Nuova istanza | «Apre il modulo per una nuova istanza del workspace, da un template o da un archivio completo, sul disco del workspace. Disattivato se il workspace non ha disco o dominio, o ha già tutte le istanze del suo limite.» |
| create-instance | dialogo | primario del dialogo | Crea | «Crea l'istanza dal template sul disco del workspace (container, database, cartelle) e ne pubblica i nomi sul web. È un job: si apre la sua pagina. Se fallisce, l'istanza non resta a metà.» (`/instances/create`, `make_instance`) |
| fa-create | dialogo | primario del dialogo | Crea dall'archivio | «Crea l'istanza dall'archivio completo: il template, i repository ai loro commit, le opzioni di Odoo e il database in produzione, neutralizzato se lasci acceso Neutralizza. È un job.» (`/instances/from-archive`) |
| fa-copy | dialogo | secondario | Copia qui | «Copia l'archivio completo dal collegamento fra gli archivi di questo workspace, poi lo scegli qui sopra. È un job. Solo amministratori della macchina.» (`/archives/fetch`) |
| fa-file | dialogo | campo file | Carica un file .zip | «Carica un archivio completo fatto da un altro OmniaCLI (Backup › Altro › Archivio completo)» |
| codice e opzioni | Istanze | link `.small` | Codice e opzioni › | «Apre le impostazioni dell'istanza: repository, opzioni di Odoo, aggiornamenti» |
| save-names | Nomi sul web | secondario, attivo con modifiche | Salva e pubblica | «Salva istanza principale, sottodomini e certificato, poi riscrive il sito nginx del workspace e chiede il certificato. È un job. Un nome senza record DNS resta fuori dal certificato.» (`/web/save`, `publish`) |
| publish-names | Nomi sul web | secondario | Pubblica di nuovo | «Riscrive il sito nginx del workspace e richiede il certificato, senza cambiare i nomi: dopo aver creato i record DNS, o se un certificato non è arrivato. È un job.» (`/web/publish`) |
| add-sub | Nomi sul web | secondario `.small` | + Sottodominio | «Aggiunge un nome <sotto>.dominio che apre l'istanza che scegli. Vale con Salva e pubblica.» |
| data-drop-sub | Nomi sul web | pericolo `.small` | Togli | «Toglie il sottodominio dalla tabella. Vale con Salva e pubblica.» |
| upload-cert | Nomi sul web | secondario | Carica e pubblica | «Controlla certificato e chiave (PEM, chiave senza password), li installa e ripubblica i nomi. L'esito dice fino a quando vale e per quali nomi.» (`/web/certificate`) |
| nuovo utente | Utenti | secondario | Nuovo utente | «Crea un utente che vede solo le istanze di questo workspace, con il ruolo che scegli qui» (`/members/save`) |
| modifica membro | Utenti | secondario `.small` | Modifica… | «Cambia il ruolo nel workspace e, se entra solo qui, la password. Non puoi cambiare il tuo ruolo.» |
| togli membro | Utenti | pericolo `.small`, conferma | Togli… | «Toglie l'utente dal workspace. Se entra solo qui, viene cancellato; se entra anche altrove, perde solo questo workspace. Non puoi togliere te stesso.» (`/members/delete`) |
| notify modifica | Servizi esterni | secondario `.small` | Modifica… | «Chi avvisare (e-mail o indirizzi di webhook) quando un'istanza del workspace non risponde e quando torna: il monitor chiede la pagina di login ogni minuto.» (`/notify`) |
| destinazioni | Servizi esterni | link | Apri › | «Apre le destinazioni dove si copiano, cifrati, i backup automatici delle istanze del workspace» |
| credenziale salva | Servizi esterni | **secondario** (oggi primario), attivo con modifiche | Salva | «Salva utente e token che OmniaCLI usa per quell'host git (github.com…) per leggere i repository privati delle istanze del workspace. Il token non si vede più e non arriva mai ai container.» (`/credentials/save`) |
| credenziale togli | Servizi esterni | pericolo `.small`, conferma | Togli… | «Toglie le credenziali di quell'host: i repository privati non si aggiornano più finché non ne metti di nuove» (`/credentials/delete`) |
| token nuovo / modifica / revoca | Servizi esterni | come §4.2 Assistenti AI | — | come §4.2, più «vede solo questo workspace» (`/tokens/save`, `/tokens/delete`) |
| salva limiti | Limiti e disco | secondario, attivo con modifiche | Salva | «Salva limiti, disco, dominio e server del workspace. Se cambi CPU, memoria o dominio, parte un job che li applica alle istanze e ripubblica i nomi.» (`/api/workspaces/save`) |
| elimina workspace | Limiti e disco | pericolo, conferma col nome | Elimina il workspace… | «Toglie il workspace: i suoi server restano, i suoi utenti non li vedono più. Il suo disco si cancella solo se è vuoto e non ha più istanze sue.» (`/api/workspaces/delete`) |

### 4.5 Elenco dei workspace (`/workspaces`)

- Restano: la testa con **Nuovo workspace** (primario), «Questa macchina», la ricerca e l'elenco.
- Il clic su una riga apre `/workspaces/<nome>` (D1). Il `title` della riga diventa «Apre il workspace <nome>».
- In fondo, chiusi: «Come funziona ▸» (oggi «Altro» più «Noti adesso») e **«Template (3) ▸»**. La tabella dei template
  e «Cerca i template pubblicati» stanno lì dentro.
- `title`:
  - «Cerca i template pubblicati»: «Legge l'indice dei template pubblicati e firmati, e ne mostra la versione accanto a quella di qui. Non installa niente.»;
  - «Installa» / «Installa di nuovo»: «Scarica il template, ne controlla la firma e lo installa su questa macchina. Le istanze già create tengono i loro file. È un job.»;
  - «Nuovo workspace»: «Crea una parte chiusa della macchina per un cliente: disco, limiti, dominio e utenti suoi.».
- **Vuoto**: «Nessun workspace: Nuovo workspace ne crea uno.» (c'è).

## 5. Il necessario

- **Stato del sistema**:
  - colonna Stato dei collegamenti (§4.1);
  - riga del disco nella testa del workspace;
  - conteggio «N modifiche» sulla barra dei Gruppi;
  - ogni azione che è un job apre la pagina del job (UX-005), come oggi.
- **Annulla**:
  - c'è una conferma su ogni azione che non si annulla: Elimina collegamento, Elimina utente, Revoca token (tutti e
    tre i tipi), Togli membro, Togli credenziali, Elimina workspace (col nome);
  - un dialogo si chiude con Annulla o Esc senza richieste POST;
  - nei Gruppi Annulla avvisa se ci sono modifiche.
- **Prevenzione dell'errore**:
  - salvataggi disattivati senza modifiche;
  - il ruolo di te stesso è disattivato, con il motivo;
  - la password nuova è rossa sotto la lunghezza minima prima del clic;
  - il nome di un'istanza si controlla in linea (1–32 caratteri a-z 0-9 -) prima di Crea;
  - «http» senza SSH: avviso rosso in linea;
  - «Nuova istanza» disattivata con il motivo.
- **Messaggio d'errore**: `refuse()` passa da `tr`, mai grezzo. Le chiavi minime:

| `refuse()` oggi | Testo |
|---|---|
| A link named X already exists / A workspace named… / User X exists | «Esiste già un collegamento (un workspace, un utente) di nome X: scegli un altro nome.» |
| The password needs at least N characters | «La password vuole almeno N caratteri.» |
| X is the last admin | «X è l'ultimo amministratore: prima dai il ruolo amministratore a un altro utente.» |
| You cannot change your own role / delete yourself | «Il tuo ruolo lo cambia un altro amministratore.» / «Non puoi eliminare te stesso.» |
| the other instance refused the token | «L'altra istanza rifiuta il token: creane uno nuovo nella sua pagina Utenti › Altre istanze e incollalo con Modifica….» |
| SSH tunnel <stato>: <dettaglio> | «Il tunnel SSH non è attivo (<dettaglio tradotto>): controlla che sul server del cliente ci sia la riga della chiave (Modifica… › Cosa mettere sul server del cliente).» |
| no OmniaCLI with links there (HTTP n) | «A quell'indirizzo non risponde un OmniaCLI (HTTP n): controlla l'indirizzo.» |
| A job is running on X: try again when it ends | «Su X lavora un job: riprova quando finisce.» |
| An instance's name is 1-32 lower case letters… | «Il nome dell'istanza: da 1 a 32 lettere minuscole, cifre e trattini.» |
| Workspace X has no storage or no disk limit… | «Il workspace X non ha disco o limite di disco: li imposta l'amministratore della macchina (Limiti e disco).» |
| X is also a user of other workspaces or of the machine… | «X entra anche altrove: la sua password la cambia l'amministratore della macchina.» |

- **Aiuto**:
  - il `title` di ogni pulsante nelle tabelle sopra;
  - i `details`: «Come funziona ▸», «Cosa mettere sul server del cliente ▸», «Cosa può fare ogni ruolo ▸», «Come si indicano i membri ▸», «Come si formano i nomi ▸», «Dove è salvato ▸»;
  - gli stati vuoti dicono il primo passo.

## 6. Criteri di accettazione (Playwright)

Come **admin della macchina** su tutte le pagine, e come **admin di un workspace** su `/workspaces/<suo>` (e su
`/links`, `/users`, `/groups`, `/workspaces`, che devono dare 403). Viewport 1440×900, lingua IT. Dati: 4 collegamenti
di cui 3 via SSH, almeno 2 utenti oltre all'admin, 1 token di collegamento, 1 workspace con un'istanza e un dominio.

1. In ogni pagina e in ogni tab, `.primary:visible` fuori da `dialog[open]` = 1 (Gruppi: 1, ed è `disabled` finché
   non si cambia un campo). Dentro `dialog[open]`: ≤ 1.
2. Ogni `button:visible`, `a.button:visible`, `summary:visible` e `[role=tab]:visible` ha `title` non vuoto e diverso
   dal testo, oppure uno `span[title]` genitore se è `disabled`. Senza aiuto: 0, compresi i «Copia» dei
   `pre.copyable` e le etichette dei membri dei Gruppi (oggi 23 + 7 + 11 + 24 + 4).
3. **Altezza del contenuto** (`main.scrollHeight`), con i `details` chiusi:
   - `/links` con 4 collegamenti ≤ 900 px;
   - `/users`, ogni tab ≤ 900 px;
   - `/groups` con i gruppi vuoti ≤ 900 px;
   - `/workspaces/<nome>`, ogni tab ≤ 1100 px;
   - `/workspaces` ≤ 1000 px.

   Sopra queste soglie serve una piega.
4. **Campi in vista** (`input:visible, select:visible, textarea:visible` fuori da `dialog[open]`):
   - `/links` ≤ 1 (la ricerca, solo oltre 8);
   - ogni tab di `/users` ≤ 1;
   - `/workspaces/<nome>` › Istanze = 0, Utenti ≤ 1, Servizi esterni = 0 salvo la riga della credenziale nuova
     (≤ 3);
   - Nomi sul web e Gruppi sono esclusi (eccezione della regola 1).
5. `/links`: entro 10 s dall'apertura ogni riga ha in Stato una delle parole «Risponde», «Non risponde», «Tunnel in
   avvio». `pre:visible` = 0 fuori da `dialog[open]`.
6. `/workspaces/<nome>`:
   - `.tabs [role=tab]:visible` = 5 per l'admin della macchina e 4 per l'admin del workspace;
   - nessun elemento con testo «Aggiungi repository», «Parametri Odoo» o «Rimuovi» nella pagina;
   - nessuna `option` con testo `developer|user|admin` (sono tradotte).
7. Nessun match di `/\d{4}-\d{2}-\d{2}/` nel testo visibile. Nessuno di
   `/\b(URL|CA|SSH…|MCP|users\.yaml|links\.yaml|groups\.yaml|host)\b/` fuori da `.mono.small`, da `details:not([open])`
   e dai `title`.
8. Ogni conferma elencata in §5 compare (`page.on("dialog")` o un `dialog[open]`) con il nome dell'oggetto nel
   testo. Annullando, nessuna richiesta POST parte. Un segreto nuovo compare in `dialog[open]` con un solo pulsante
   «Ho copiato il token».
9. Errori provocati, con il testo italiano di §5 e nessuna parola inglese:
   - Nuovo utente con password di 3 caratteri;
   - Prova di un collegamento con token sbagliato;
   - Nuova istanza col nome «A_B».
10. Nessuna chiamata ad `alert(`: `page.on("dialog")` di tipo `alert` = 0 durante il caricamento del certificato.

## 7. Ordine di implementazione (per impatto)

1. **Collegamenti**: righe lette, dialogo Modifica con SSH e «Cosa mettere sul server ▸», Stato all'apertura (D2).
   È la pagina più lunga, da 3709 a meno di 900 px. Criteri 1–5.
2. **Pagina del workspace a tab**: via i doppioni di UX-006 (repository, parametri, Rimuovi); Nuova istanza in
   dialogo; un solo primario. Criteri 1, 3, 6.
3. **Regole comuni su tutte e cinque le pagine**:
   - `title` ovunque, anche su «Copia» in `base.html`;
   - `refuse()` tradotti;
   - dialogo del segreto;
   - niente `alert`.

   Criteri 2, 7, 8, 9, 10.
4. **Utenti a tab** con le righe lette e i dialoghi. Criteri 3, 4.
5. **Gruppi** (barra d'azione, un primario) ed **elenco dei workspace** (Template piegato, clic sulla pagina: D1).

## 8. Decisioni di Carlo

1. **D1 — Una pagina per workspace.** Oggi il clic su una riga di `/workspaces` apre il modulo della macchina
   (`?edit=`), e da lì «Utenti del workspace» apre `/workspaces/<nome>`. Per alzare il limite di disco di `odoo19` e
   poi aggiungere un utente si passa da due pagine, con due «Utenti» diversi.
   - **Raccomandata: il clic apre `/workspaces/<nome>`, e il modulo della macchina diventa il tab «Limiti e disco»**,
     visibile solo all'admin della macchina. Gli utenti si gestiscono solo nel tab Utenti: la textarea `nome ruolo`
     va via. Nessuna route nuova: il tab chiama `/api/workspaces/save` come oggi, con i membri presi dal workspace.
   - Alternativa: resta `?edit=`, e la pagina del workspace ha il link «Limiti (amministratore della macchina) ›».
     Le due pagine e i due elenchi di utenti restano.
2. **D2 — Lo stato dei collegamenti all'apertura.**
   - **Raccomandata: sì.** Il JS chiama `/api/links/<nome>/test` per ogni collegamento, in parallelo. Con 4
     collegamenti sono 4 richieste; una che non risponde aspetta al massimo il `LIST_TIMEOUT` e non blocca le altre.
     Carlo vede subito che `ridix` non risponde, senza premere 4 volte «Prova».
   - Alternativa: lo stato si vede solo dopo «Prova». La colonna dice «Da provare» per i collegamenti diretti, e per
     quelli SSH solo lo stato del tunnel che c'è oggi.
3. **D3 — La parola per i token.**
   - **Raccomandata: «token» resta per i collegamenti fra OmniaCLI** (è la parola che l'altra pagina Collegamenti
     chiede), **«chiave» per gli assistenti AI** («Nuova chiave per un assistente»), con `MCP` in `.small mono`.
   - Alternativa: «token» ovunque, compreso «Nuovo token MCP» come oggi. È più corto, ma `MCP` resta un'etichetta.
4. **D4 — Utenti: tab o pagina unica.**
   - **Raccomandata: tre tab** (Persone · Altre istanze · Assistenti AI). Ogni tab ha la sua tabella e un primario,
     e oggi la pagina passa la piega di 338 px per due tabelle quasi vuote.
   - Alternativa: pagina unica, Persone aperta e le altre due in `details` «Altre istanze (1) ▸» e
     «Assistenti AI (0) ▸». Un clic in meno per chi le usa, ma il primario è uno solo per la pagina (Nuovo utente) e
     le altre due aggiunte diventano secondarie.

**Funzioni di backend che servirebbero (fuori da questo contratto)**
- L'**ultimo accesso** di ogni utente e di ogni token nella riga: il registro degli accessi lo ha, ma `/users` non
  lo passa al template.
- **`/api/links/delete` che conti cosa lo usa**, come fa già `/api/links/rename`: istanze copiate da lì e
  pubblicazioni. La conferma potrebbe dire «2 istanze sono state copiate da qui».
- L'**ultima prova riuscita** di un collegamento, salvata, così lo Stato ha una data anche prima della risposta.

## Aggiunta del 20/11/2026: la pagina Pubblicazioni

Stessa origine e stesse regole (§2); i criteri qui sotto si aggiungono a §6, le decisioni a §8.

### 4.6 Pubblicazioni (`/pullers`)

Oggi (20/11, admin, 1440×900, IT) la pagina è alta 1409–1469 px. Ha 24 campi aperti nelle righe e 3 pannelli in colonna, ognuno con «Altro», una ricerca sempre visibile e una riga «nuovo» in fondo. I pulsanti con `title` sono 7, il primario 1 («Aggiungi puller»). È l'ultima pagina di amministrazione contro la regola 1.

Altri difetti di oggi:
- le etichette sono «Puller», «Producer», «Consumer», «Collegamento al relay», «Connessione»;
- il campo Chiave è schiacciato a una riga di 6 px (schermate);
- la chiave nuova compare in un banner prima di essere salvata;
- dopo il salvataggio di una pubblicazione, Annulla del `confirm("Salvato. Attenzione…")` salta il ricaricamento;
- «Togli la chiave» salva anche le altre modifiche non salvate della riga.

**Le tre cose, dette dal codice**

| Oggi | Che cos'è (dal codice) | Si dice |
|---|---|---|
| Puller («Inoltrati da qui», `PullerDef`, `puller.py`) | una casella su **questa** macchina, l'«ufficio postale» del docstring. Un OmniaCLI che non si raggiunge da fuori ci lascia rapporti e backup e prende i comandi. Gli OmniaCLI che lo gestiscono leggono i rapporti e lasciano i comandi. Entrano tutti con un accesso di Utenti › Altre istanze (`settings.links`). Con una chiave, questa macchina non legge né cambia quello che passa | **Casella** (`puller` in `.small mono` dopo il nome, solo nel dialogo) |
| Producer / Consumer | il collegamento in entrata di chi pubblica e di chi gestisce | **Chi pubblica** / **Chi gestisce** |
| Pubblicazione («Pubblicate da qui», `Publication`, `publisher.py`) | un'istanza di **questa** macchina che chiama la macchina di inoltro tramite un collegamento della pagina Collegamenti (`link_targets`). Manda i rapporti (stato, dischi, carico, job) e i backup, e prende solo i comandi permessi, nei loro orari. Nessuno entra in questa macchina | **Istanza pubblicata** |
| Connessione («Collegati da qui», `PullerConnection`, `consumer.py`) | **questa** macchina gestisce un'istanza che un altro OmniaCLI pubblica su una casella. Ha una sua pagina (`/pullers/connections/<nome>`) e compare nella barra in alto (`base.html` riga 525) | **Istanza gestita da qui** |
| Relay | l'OmniaCLI raggiungibile che tiene la casella | **Macchina di inoltro** |

Persona: l'integratore, admin della macchina; tutte le route sono `Depends(admin)`. Desktop. Ogni macchina di solito ha un solo ruolo dei tre: quella del cliente pubblica, quella pubblica inoltra, l'orchestratore gestisce.

**Lavoro primario:** «Le istanze che passano da un inoltro arrivano? Ne aggiungo una.»

**Testa:** «Pubblicazioni», con la riga «Le istanze che non si raggiungono da fuori: chi le pubblica, chi fa da inoltro, chi le gestisce. Nessuno entra nella macchina del cliente: è lei che chiama.» Il primario cambia con il tab e ce n'è sempre uno solo, come in Utenti (§4.2).

**Tre tab** (`.tabs`, il tab in `#hash`), nell'ordine in cui passano i dati, **Pubblicate da qui (N) · Inoltrate da qui (N) · Gestite da qui (N)**:
- senza `#hash` si apre il primo tab con righe;
- con tutti e tre a 0 si apre il primo, e «Come funziona ▸» è aperto (rete di sicurezza);
- un tab a 0 resta visibile con il suo conteggio: sono 3 e si vede subito dove sta il lavoro.

**Sopra la piega, in ogni tab:** una riga di descrizione, la tabella e, solo oltre 8 righe, la ricerca. In fondo alla pagina, chiusi, «Come funziona ▸» (i tre «Altro» di oggi, riscritti con le parole sopra) e «Dove è salvato ▸»: i tre file in `.small mono`, con «leggibili solo dal proprietario: contengono le chiavi» per pubblicazioni e istanze gestite.

#### A. Pubblicate da qui (primario **Pubblica un'istanza**)

Descrizione: «Le istanze di questa macchina che mandano i rapporti a una casella di una macchina di inoltro, e ne prendono i comandi che permetti.»

| Colonna | Contenuto |
|---|---|
| Istanza | «odoo19-odoo · produzione» (da `server/role`), link alla pagina dell'istanza. Se non è attiva qui: badge rosso «L'istanza non è attiva: non parte niente» |
| Dove | «tramite **ber** · casella **api-prod**». Con `link_missing`: badge rosso «Manca il collegamento ber: Modifica…» |
| Comandi permessi | a parole: «Backup sempre · Riavvia 22:00–06:00»; nessuno: «Nessun comando: solo rapporti». Sotto, in `.small`: «Con chiave» oppure, ambra, «In chiaro». Rosso se Avvia/Ferma/Riavvia sono permessi senza chiave: «Avvia, Ferma senza chiave: chi controlla la macchina di inoltro può mandarli» (è il `warning` di `/api/publications/save`, calcolato nel template) |
| Stato | **Arriva** (verde) «ultimo rapporto gg/mm/aaaa hh:mm»; in `.small` «N comandi presi». **Non arriva** (rosso) con la causa tradotta in una riga (tabella sotto). **In partenza** (grigio, `ok` è `None`) |
| Azioni | Modifica… · Smetti di pubblicare… |

**Dialogo «Pubblica un'istanza» / «Modifica la pubblicazione di <istanza>»**, in quest'ordine:
1. Istanza: un menu, solo nel nuovo, con le istanze non ancora pubblicate scritte come nella riga; nel Modifica è testo.
2. Macchina di inoltro: un menu dei Collegamenti. Accanto, il link «Collegamenti ›».
3. Casella: il nome che la casella ha sulla macchina di inoltro. Si controlla in linea: lettere, cifre e . _ -
4. Comandi permessi: quattro caselle (Backup, Avvia, Ferma, Riavvia). Il campo Orari compare solo accanto a una casella spuntata: segnaposto «sempre», formato 22:00-06:00 controllato in linea. Avviso rosso in linea, prima di Salva, se Avvia, Ferma o Riavvia è spuntato senza chiave.
5. Chiave, come stato a parole:
   - senza chiave: «Nessuna chiave: tutto passa in chiaro sulla macchina di inoltro» e il secondario **Crea una chiave**;
   - con chiave: «Chiave impostata», con i secondari **Sostituisci la chiave** e **Togli la chiave**;
   - dopo Crea o Sostituisci la riga dice «Chiave nuova: vale al salvataggio». La chiave non si vede nel dialogo;
   - dopo Togli dice, in rosso, «Al salvataggio tutto passa in chiaro», e Salva diventa **Salva senza chiave**;
   - tutti e tre sono cambi del dialogo: non partono richieste POST.
6. `details` **«Avanzate ▸»**, chiuso: «Incolla una chiave esistente» (campo `mono`), per ripubblicare con la chiave che i gestori hanno già.
7. **Salva**, primario del dialogo, disattivato finché non cambi · Annulla.
8. Dopo Salva:
   - se la chiave è nuova, si apre il **dialogo del segreto** (regola 3): «La chiave di <istanza>. Incollala con Modifica… nella scheda Gestite da qui di ogni macchina che gestisce questa istanza: senza, i loro rapporti vengono rifiutati.» Poi Copia e **Ho copiato la chiave**;
   - poi la pagina si ricarica sempre. Il `confirm()` di oggi sparisce: l'avviso è già in linea, e dopo il salvataggio resta nella colonna Comandi permessi.

**Vuoto:** «Nessuna istanza pubblicata. Si pubblica un'istanza quando la sua macchina non si raggiunge da fuori: è lei a chiamare una macchina di inoltro. Serve prima un collegamento verso quella macchina, nella pagina Collegamenti.»

Il primario è disattivato, con il motivo nel `title` dello `span`:
- senza collegamenti: «Serve un collegamento verso la macchina di inoltro: aggiungilo in Collegamenti»;
- senza istanze libere: «Tutte le istanze di questa macchina sono già pubblicate».

#### B. Inoltrate da qui (primario **Nuova casella**)

Descrizione: «Le caselle che questa macchina tiene per altri OmniaCLI: chi pubblica ci lascia rapporti e backup, chi gestisce ci lascia i comandi.»

| Colonna | Contenuto |
|---|---|
| Casella | il nome. Se manca un accesso (`missing`): badge rosso «Manca l'accesso di <nomi>: Utenti › Altre istanze» |
| Chi pubblica | il nome, con un pallino e una parola: **in linea** (verde, contatto negli ultimi 2 minuti) · «ultimo contatto gg/mm/aaaa hh:mm» (grigio) · **mai visto** |
| Chi gestisce | i nomi, ciascuno in `.small` con «gg/mm/aaaa hh:mm» oppure «mai visto» |
| In transito | «2 comandi in attesa · 1 scaduto · 1 file, 230,4 MiB»; niente: «Niente in attesa» |
| Azioni | Modifica… · Elimina… |

**Dialogo «Nuova casella» / «Modifica <nome>»**, in quest'ordine:
1. Nome, solo nel nuovo. Hint: «lo stesso nome va scritto sulla macchina che pubblica e su quelle che gestiscono; lettere, cifre e . _ -». Controllo in linea, al massimo 64 caratteri. Nel Modifica è testo: non c'è una route per rinominare.
2. Chi pubblica: un menu degli accessi di Utenti › Altre istanze.
3. Chi gestisce: **caselle di spunta** sugli stessi accessi, senza chi pubblica. Prendono il posto del campo di testo con le virgole, e insieme al punto 2 tolgono di mezzo i due errori di oggi «X non può pubblicare e gestire» e «almeno uno». Il rosso in linea compare finché non ce n'è almeno una.
4. `details` **«Avanzate ▸»**, chiuso: Ore di conservazione (1–720, predefinito 24) e File più grande, MiB (predefinito 10240, con «10 GiB» accanto). Rete di sicurezza: è aperto se un valore è diverso dal predefinito.
5. **Salva**, primario del dialogo, disattivato finché non cambi · Annulla.

**Vuoto:** «Nessuna casella. Una casella serve quando questa macchina si raggiunge da fuori e fa da inoltro per un'altra che non si raggiunge. Prima crea in Utenti › Altre istanze l'accesso di chi pubblica e di chi gestisce (basta il ruolo utente).»

Il primario è disattivato con meno di due accessi: «Servono almeno due accessi in Utenti › Altre istanze: uno per chi pubblica e uno per chi gestisce.»

#### C. Gestite da qui (primario **Aggiungi un'istanza da gestire**)

Descrizione: «Le istanze di altri OmniaCLI che gestisci da qui attraverso una casella: le trovi anche nella barra in alto.»

| Colonna | Contenuto |
|---|---|
| Istanza | il nome, link a `/pullers/connections/<nome>` |
| Dove | «tramite **ber** · casella **api-prod**». Con `link_missing`: badge rosso «Manca il collegamento ber: Modifica…» |
| Protezione | «Con chiave» · «In chiaro» (ambra) |
| Stato | **Rapporti letti** (verde) gg/mm/aaaa hh:mm · **Rapporti rifiutati** (rosso, quando l'errore comincia con «a report refused») con la causa · **Non arriva niente** (rosso) con la causa · **In attesa del primo rapporto** (grigio) |
| Azioni | Modifica… · Elimina… |

**Dialogo «Aggiungi un'istanza da gestire» / «Modifica <nome>»**:
1. Nome, solo nel nuovo: «come compare nella barra in alto»; controllo in linea.
2. Macchina di inoltro: un menu dei Collegamenti.
3. Casella.
4. Chiave:
   - nel nuovo: «Incolla la chiave della pubblicazione; vuota: in chiaro»;
   - nel Modifica: «invariata», con il secondario **Togli la chiave**. È uno stato del dialogo: rosso in linea «Al salvataggio i rapporti cifrati vengono rifiutati», e Salva diventa **Salva senza chiave**.
5. **Salva**, disattivato finché non cambi · Annulla.

**Vuoto:** «Nessuna istanza gestita da qui. Sulla macchina del cliente qualcuno pubblica un'istanza su una casella; qui la aggiungi con il nome della casella e la sua chiave. Serve un collegamento verso la macchina di inoltro, nella pagina Collegamenti.»

Il primario è disattivato senza collegamenti: «Serve un collegamento verso la macchina di inoltro: aggiungilo in Collegamenti».

**Lista HIDE**

| Elemento | Dove va | Rete di sicurezza |
|---|---|---|
| I 24 campi delle righe (menu, chiavi, ore, MiB, caselle e orari dei comandi) | dialogo Modifica | ogni valore resta letto nella riga, a parole |
| Le tre righe «nuovo» in fondo alle tabelle | dialogo dal primario del tab | stato vuoto con il primo passo; primario disattivato con il motivo |
| Banner «La chiave nuova» con il `pre` | dialogo del segreto, dopo Salva | la riga dice «Con chiave» o «In chiaro» |
| Pulsanti «Nuova chiave» e «Togli la chiave» nelle righe | dialogo, come stati che non salvano da soli | Salva cambia etichetta; avviso rosso in linea |
| Ore di conservazione, file più grande | Avanzate ▸ del dialogo della casella | aperto se diverso dal predefinito |
| «Incolla una chiave esistente» della pubblicazione | Avanzate ▸ | — |
| I tre «Altro» e i percorsi dei file | «Come funziona ▸», «Dove è salvato ▸» in fondo alla pagina | «Come funziona ▸» aperto se tutti i tab sono vuoti |
| Le tre ricerche | solo oltre 8 righe nel tab | — |
| Due pannelli su tre | tab | conteggio (N) su ogni tab; si apre il primo con righe |

**Testi**

| Oggi | Diventa |
|---|---|
| Puller (titolo, briciole, scheda di Impostazioni) | Pubblicazioni (P2) |
| Inoltrati da qui / Pubblicate da qui / Collegati da qui | Inoltrate da qui / Pubblicate da qui / Gestite da qui |
| Puller (colonna) | Casella |
| Producer / Consumer | Chi pubblica / Chi gestisce |
| Collegamento al relay | Macchina di inoltro (nel dialogo); «tramite <nome>» (nella riga) |
| Connessione | Istanza |
| Adesso | Stato / In transito |
| Ore di conservazione · File più grande, MiB | invariati, dentro Avanzate ▸ |
| «nessuna chiave: in chiaro» | In chiaro |
| relay raggiunto / rapporti letti | Arriva / Rapporti letti |
| Aggiungi puller / Pubblica / Collega | Nuova casella / Pubblica un'istanza / Aggiungi un'istanza da gestire |
| Elimina (pubblicazione) | Smetti di pubblicare… |
| Altro (×3) | Come funziona ▸ (uno, in fondo) |

| Pulsante | Tab | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| tab | — | tab | Pubblicate da qui (N) | «Le istanze di questa macchina che mandano i rapporti a una casella di una macchina di inoltro» |
| tab | — | tab | Inoltrate da qui (N) | «Le caselle che questa macchina tiene per altri OmniaCLI» |
| tab | — | tab | Gestite da qui (N) | «Le istanze di altri OmniaCLI che gestisci da qui attraverso una casella» |
| pubblica | Pubblicate | **primario** | Pubblica un'istanza | «Manda i rapporti di un'istanza di questa macchina (stato, dischi, carico, job) a una casella di una macchina di inoltro, e prende da lì solo i comandi che permetti. Nessuno entra in questa macchina: è lei che chiama. Serve un collegamento verso la macchina di inoltro, nella pagina Collegamenti.» (`/api/publications/save`, `new`) |
| modifica pubblicazione | Pubblicate | secondario `.small` | Modifica… | «Cambia macchina di inoltro, casella, comandi permessi con i loro orari e chiave. Vale dal giro successivo, entro mezzo minuto.» |
| smetti | Pubblicate | pericolo `.small`, conferma | Smetti di pubblicare… | «Smette di mandare rapporti e backup e di prendere comandi per questa istanza. L'istanza non si tocca, e la casella sulla macchina di inoltro resta. Chiede conferma.» (`/api/publications/delete`). Conferma: «Smettere di pubblicare <istanza>? Chi la gestisce non riceve più rapporti né può mandarle comandi.» |
| crea chiave | dialogo pubblicazione | secondario `.small` | Crea una chiave | «Prepara una chiave nuova che cifra rapporti, comandi e backup: la macchina di inoltro non può più leggerli né cambiarli. Vale solo dopo Salva, e allora te la mostra una volta: va data a ogni macchina che gestisce l'istanza.» (`/api/publications/key`, che non salva niente) |
| sostituisci chiave | dialogo pubblicazione | secondario `.small` | Sostituisci la chiave | «Come Crea una chiave. Al salvataggio la chiave vecchia smette di valere: chi gestisce con quella rifiuta i rapporti finché non mette la nuova.» |
| togli chiave | dialogo pubblicazione | pericolo `.small` | Togli la chiave | «Al salvataggio tutto passa in chiaro sulla macchina di inoltro, e chi gestisce con la chiave rifiuta i rapporti finché non la toglie anche lui. Da solo non salva niente.» (`remove_key`) |
| caselle dei comandi | dialogo pubblicazione | casella (sull'etichetta) | Backup / Avvia / Ferma / Riavvia | «Chi gestisce può chiedere un backup di questa istanza: si fa qui e va alla casella, cifrato se c'è la chiave.» / «Chi gestisce può avviare (fermare, riavviare) questa istanza.» |
| orari | dialogo pubblicazione | campo | Orari | «Le ore in cui il comando si prende, sull'orologio di questa macchina, per esempio 22:00-06:00. Vuoto: sempre. Fuori orario il comando si rifiuta subito.» |
| salva pubblicazione | dialogo | primario del dialogo | Salva / Salva senza chiave | «Salva la pubblicazione. Disattivato finché non cambi qualcosa.» |
| nuova casella | Inoltrate | **primario** | Nuova casella | «Apre una casella su questa macchina. Un OmniaCLI che non si raggiunge (chi pubblica) ci lascia rapporti e backup e ne prende i comandi; quelli che lo gestiscono leggono i rapporti e lasciano i comandi. Entrano tutti con un accesso di Utenti › Altre istanze (basta il ruolo utente).» (`/api/pullers/save`, `new`) |
| modifica casella | Inoltrate | secondario `.small` | Modifica… | «Cambia chi pubblica, chi gestisce, le ore di conservazione e il file più grande. Il nome non cambia: fa parte degli indirizzi che usano le altre macchine. Comandi e file in attesa restano; chi togli non entra più.» |
| elimina casella | Inoltrate | pericolo `.small`, conferma | Elimina… | «Cancella la casella con i comandi in attesa, i rapporti e i file. Chi pubblica e chi gestisce si sentono rispondere "casella inesistente" finché non la ricrei; le loro impostazioni non cambiano. Chiede conferma.» (`/api/pullers/delete`, `forget`). Conferma, dai dati della riga: «Eliminare la casella <nome>? Se ne vanno anche 2 comandi in attesa e 1 file (230,4 MiB).» |
| salva casella | dialogo | primario del dialogo | Salva | «Salva la casella. Ogni nome scelto deve avere un accesso in Utenti › Altre istanze. Disattivato finché non cambi qualcosa.» |
| aggiungi gestita | Gestite | **primario** | Aggiungi un'istanza da gestire | «Gestisce da qui un'istanza che un altro OmniaCLI pubblica su una casella: ne legge i rapporti e le lascia i comandi. L'istanza compare nella barra in alto. Serve un collegamento verso la macchina di inoltro (pagina Collegamenti) e, se la pubblicazione ne ha una, la sua chiave.» (`/api/connections/save`, `new`) |
| nome istanza gestita | Gestite | link | `<nome>` | «Apre l'istanza: stato, carico, job e comandi, come li dicono i suoi rapporti» |
| modifica gestita | Gestite | secondario `.small` | Modifica… | «Cambia macchina di inoltro, casella e chiave. La chiave lasciata vuota resta quella salvata.» |
| togli chiave gestita | dialogo gestita | pericolo `.small` | Togli la chiave | «Al salvataggio i rapporti cifrati vengono rifiutati. Da solo non salva niente.» (`remove_key`) |
| elimina gestita | Gestite | pericolo `.small`, conferma | Elimina… | «Smette di gestire l'istanza da qui, che sparisce dalla barra in alto. Quello che è già arrivato resta sul disco. Sulla macchina di inoltro e su quella del cliente non cambia niente. Chiede conferma.» (`/api/connections/delete`). Conferma: «Smettere di gestire <nome> da qui?» |
| ho copiato | dialogo del segreto | primario del dialogo | Ho copiato la chiave | «Chiude: la chiave non si vedrà più. Se la perdi, Sostituisci la chiave e dalla di nuovo a chi gestisce.» |
| Annulla | ogni dialogo | secondario | Annulla | «Chiude senza salvare» |
| come funziona | fondo | `summary` | Come funziona | «Chi pubblica, chi inoltra, chi gestisce: cosa passa, cosa può leggere la macchina di inoltro, per quanto resta» |
| dove è salvato | fondo | `summary` | Dove è salvato | «I tre file di questa macchina con caselle, pubblicazioni e istanze gestite, chiavi comprese» |

**Messaggi d'errore** (`refuse()` e stati, da `tr`; il prefisso `pullers.<nome>.` / `publications.<…>.` / `connections.<…>.` di `ConfigError` non si mostra)

| Oggi | Testo |
|---|---|
| A puller named X already exists / A puller connection named X already exists | «Esiste già una casella (un'istanza gestita) di nome X: scegli un altro nome.» |
| X is already published | «X è già pubblicata: cambiala con Modifica….» |
| No puller X / X is not published / No puller connection X (404) | «X non c'è più: ricarica la pagina.» |
| No instance X | «L'istanza X non c'è su questa macchina.» |
| …: a name is letters, digits and . _ - (it is part of URLs) / … is not a name of letters, digits and . _ - | «Il nome: lettere, cifre e . _ - (fa parte degli indirizzi).» |
| .producer: the name of a link of the Users page | «Scegli chi pubblica fra gli accessi di Utenti › Altre istanze.» |
| .consumers: at least one | «Scegli almeno una macchina che gestisce.» |
| X cannot be the producer and a consumer | «X non può pubblicare e gestire la stessa casella.» |
| keep_hours must be a whole number from 1 to 720 | «Ore di conservazione: un numero intero da 1 a 720.» |
| max_file_mib must be a whole number from 1 to 1048576 | «File più grande: un numero intero di MiB da 1 a 1048576.» |
| No incoming link X: create it on the Users page | «X non ha un accesso a questa macchina: crealo in Utenti › Altre istanze.» |
| No link X: add the relay on the Links page | «Non c'è il collegamento X: aggiungi la macchina di inoltro in Collegamenti.» |
| window must be 'HH:MM-HH:MM' / invalid time …, expected HH:MM | «Orari di <comando>: nel formato 22:00-06:00, oppure vuoto per sempre.» |
| .key: a puller key starts with opk1- / holds 32 bytes / not base64 text | «Questa non è una chiave di pubblicazione intera: copiala di nuovo, comincia con opk1-.» |
| Cannot write <file>: … (500) | «Non riesco a scrivere il file (<motivo>): niente è cambiato.» |
| warning «start, stop exposed without a key…» | «Avvia, Ferma senza chiave: chi controlla la macchina di inoltro può mandarli.» |
| stato: X is not a live instance here | «L'istanza non è attiva qui: non parte niente.» |
| stato: no link X on the Links page | «Manca il collegamento X: Modifica….» |
| stato: X answered HTTP 404: No puller Y | «La macchina di inoltro non ha la casella Y: va creata lì (Inoltrate da qui).» |
| stato: X answered HTTP 403: … is the consumer of puller Y, not its producer | «Su X questa macchina non è chi pubblica la casella Y: va cambiato lì.» |
| stato: X: <errore di rete> | «La macchina di inoltro X non risponde (<causa tradotta>).» |
| stato: a report refused: the message does not open with this puller's key… | «Rapporti rifiutati: la chiave non è quella della pubblicazione (o la casella è un'altra). Incollala di nuovo con Modifica….» |
| stato: a report refused: a message in clear on a puller with a key | «Rapporti rifiutati: arrivano in chiaro e qui c'è una chiave. Togli la chiave qui, o mettila nella pubblicazione.» |
| stato: a report refused: an encrypted message, and no key for this puller here | «Rapporti rifiutati: arrivano cifrati e qui non c'è la chiave. Incollala con Modifica….» |

**Il necessario**
- **Stato del sistema:** Stato a parole in ogni riga di Pubblicate e Gestite, «in linea» e «In transito» in Inoltrate. Il conteggio (N) è su ogni tab. Le date sono `gg/mm/aaaa hh:mm`: oggi `toLocaleString()` mette i secondi e la virgola.
- **Annulla:**
  - c'è una conferma, con il nome dell'oggetto, su Elimina casella, Smetti di pubblicare ed Elimina istanza gestita;
  - Crea, Sostituisci e Togli la chiave non partono finché non premi Salva;
  - Annulla o Esc chiudono il dialogo senza richieste POST.
- **Prevenzione dell'errore:**
  - Salva è disattivato senza modifiche;
  - chi pubblica e chi gestisce si scelgono da elenchi (niente nomi a mano);
  - nomi e orari si controllano in linea;
  - «Avvia/Ferma senza chiave» è rosso prima di Salva;
  - Salva cambia etichetta quando toglie la chiave;
  - i primari sono disattivati con il motivo.
- **Messaggio d'errore:** la tabella sopra, mai `data.error` grezzo (regola 6). L'esito di un errore resta nella fascia `#error` in testa; con il dialogo aperto va sotto il Salva del dialogo.
- **Aiuto:** il `title` di ogni pulsante; «Come funziona ▸»; gli stati vuoti dicono il primo passo e su quale pagina.

#### Righe da aggiungere a §6

- **Dati:** sulla macchina di prova, 2 accessi in Utenti › Altre istanze, 1 collegamento verso una macchina di inoltro, 2 caselle, 2 pubblicazioni (una con chiave, una in chiaro con Riavvia permesso), 2 istanze gestite.
- **Criterio 1:** su `/pullers`, in ogni tab, `.primary:visible` fuori da `dialog[open]` = 1 (oggi 1 per tutta la pagina, ma con 3 pannelli). Dentro `dialog[open]`: ≤ 1.
- **Criterio 2:** su `/pullers`, compresi i 3 tab, i 2 `summary` e i pulsanti dei dialoghi aperti, pulsanti senza aiuto = 0.
- **Criterio 3:** su `/pullers`, ogni tab ≤ 900 px con i `details` chiusi (oggi 1409–1469 px per la pagina intera).
- **Criterio 4:** su `/pullers`, ogni tab ≤ 1 campo in vista fuori da `dialog[open]` (la ricerca, solo oltre 8 righe). Oggi 24. `pre:visible` fuori da `dialog[open]` = 0.
- **Criterio 7:** su `/pullers`, nessun match di `/\b(puller|producer|consumer|relay)\b/i` fuori da `.mono.small`, da `details:not([open])` e dai `title`.
- **Criterio 8:** una chiave nuova compare solo in `dialog[open]` **dopo** la risposta di `/api/publications/save`, con un solo pulsante «Ho copiato la chiave». Crea, Sostituisci e Togli la chiave non fanno partire nessuna richiesta verso `/save`.
- **Criterio 11 (nuovo):** durante il salvataggio di una pubblicazione con Riavvia e senza chiave, `page.on("dialog")` = 0, la pagina si ricarica e la riga mostra in rosso «Riavvia senza chiave». Senza `#hash` si apre il primo tab con righe; con tutti i file vuoti si apre «Pubblicate da qui» con «Come funziona ▸» aperto.
- **Criterio 9:** aggiungere tre errori provocati: Nuova casella senza nessun gestore (il Salva resta disattivato, con il rosso in linea); orari «25:00-06:00» (testo italiano in linea); Aggiungi un'istanza da gestire con la chiave «abc».

#### Decisioni di Carlo (da aggiungere a §8)

1. **P1: la parola per «puller».**
   - **Raccomandata: «casella»**, perché il codice la descrive come un ufficio postale dove si lasciano e si prendono comandi, rapporti e file. «puller» resta in `.small mono` nel dialogo.
   - Alternativa: «puller» come oggi. È più corto e uguale nelle due lingue, ma il criterio 7 va tolto per questa parola.
2. **P2: il titolo della pagina.**
   - **Raccomandata: «Pubblicazioni»**, il nome che usi già. La riga sotto il titolo nomina tutti e tre i ruoli.
   - Alternativa: «Inoltro», più neutro sui tre tab ma meno parlante per chi pubblica.
   - In tutti e due i casi cambiano anche le briciole di `connection.html`, la scheda di Impostazioni e il `title` della voce nella barra in alto.
3. **P3: il nome di un'istanza gestita.**
   - **Raccomandata: non si cambia** (nessuna route); per cambiarlo si elimina e si aggiunge di nuovo. Quello che è arrivato resta sul disco sotto il nome vecchio.
   - Alternativa: una route di rinomina (sotto).

**Funzioni di backend che servirebbero (fuori da questo contratto)**
- Rinominare una casella o un'istanza gestita, come fa `/api/links/rename` per i collegamenti.
- **`/api/links/rename` sposta le pubblicazioni ma non le istanze gestite** (`app.py` intorno alla riga 4449). Dopo una rinomina, una riga di «Gestite da qui» mostra «Manca il collegamento X». Il badge `link_missing` è la rete di sicurezza, ma è un difetto da segnalare a parte.
- Lo stato di una pubblicazione (ultimo rapporto, comandi presi) vive in memoria: dopo un riavvio di OmniaCLI la riga dice «In partenza» fino al primo giro. Salvarlo darebbe la data anche prima.

**Non verificato (Pubblicazioni)**

1. La soglia «in linea = contatto negli ultimi 2 minuti» la deduco da `WAIT = 25 s` del long-poll. Non ho letto `RETRY` né misurato.
2. Non ho controllato se un admin di workspace riceve 403 su `/pullers` come sulle altre pagine. Né se `/pullers/connections/<nome>`, che non ha `Depends(admin)`, è aperta a tutti i ruoli.
3. «Vale dal giro successivo, entro mezzo minuto»: è letto in `cycle()`, che rilegge la pubblicazione a ogni giro. Che un job già partito da un comando continui dopo Smetti di pubblicare non l'ho verificato, e per questo il `title` non lo promette.
4. Le altezze sotto i 900 px sono stime (righe di circa 56 px), non misure. Le traduzioni degli errori di rete (`describe_error`) non le ho lette una per una.

## Stato (20/11/2026 notte)

Fatti: Collegamenti (2026.1.5446), Utenti, Gruppi, pagina del Workspace ed elenco (2026.1.5447). Misurato con Playwright
come admin a 1440×900: un primario per pagina e per tab, 0 pulsanti e `summary` senza `title` fuori dai dialoghi;
Collegamenti 844 px (da 3709), Workspace al più 969 px per tab (da 2883), nessun campo nelle righe.

Scostamenti, voluti:
- **D1 non presa**: il Workspace ha 4 tab; per l'admin della macchina c'è il link «Limiti e disco ›» al modulo di
  oggi, e il clic sulla riga dell'elenco apre ancora quel modulo.
- Il branch del test e il nome della cartella di un repository, che si davano dal Workspace, stanno ora nella pagina
  dell'istanza: Impostazioni › Codice › Aggiungi un repository › Avanzate.
- Gruppi: il conteggio delle modifiche è una stima (gruppi cambiati di posto, nome o membri).
- Utenti: «token» per i collegamenti, «chiave» per gli assistenti AI (D3 raccomandata), tre tab (D4 raccomandata);
  lo stato dei collegamenti si legge all'apertura (D2 raccomandata).

Restano aperti: D1; i testi d'errore di §5 non ancora tutti nel catalogo; le misure come admin di un workspace e a
375×812; l'indirizzo per gli assistenti AI, che visto attraverso un collegamento mostra quello interno del tunnel.

## Non verificato

- I `title` di Elimina collegamento e di Elimina utente non promettono di chiudere le sessioni già aperte, né
  di avvisare le istanze copiate: non ho letto `store_access` né i consumatori di `from_link` fino in fondo.
- «Ogni istanza ha una produzione e un test»: la pagina lo dice, ma `workspace_instance` commenta «the instances made
  now have no test». I `title` di Crea e di Rimuovi non nominano il test finché non si verifica cosa crea
  `create_instance` oggi.
- Gruppi: «Server noti adesso: ancora nessuno» mentre `/workspaces` elenca `odoo19-odoo`. `known.servers` usa
  `active_servers()`; non ho controllato se `odoo19-odoo` è attivo, quindi lo stato vuoto potrebbe essere vero o un
  difetto.
- Le altezze dopo il cambio sono stime da schermata (righe da ~56 px), non misure. I `title` dei pulsanti del macro
  `_credentials.html` e del modulo `?edit=` li ho scritti leggendo le route, non provati a schermo. Non ho misurato
  come admin di un workspace né a 375×812.
- Che la pagina dell'istanza copra davvero, per produzione **e** test, i parametri che oggi si cambiano dal
  workspace (UX-006 › Opzioni di Odoo usa la stessa route `parameters/save`): da confermare prima di togliere i
  `details` «Parametri Odoo di …».

## Tracciabilità

Obiezione di Carlo del 20/11/2026 («gli manca proprio l'usabilità»). Misura Playwright del 20/11 (schermate
`ux7-*.png`). Estende UX-001…006 (`UX-001-hosting-panel-parity.md`, `UX-006-pagina-istanza-dopo-20-11.md`): regole
comuni, un primario, `title` dal codice, date `gg/mm/aaaa hh:mm`. Nessun ticket `T`/`HT` collegato.
