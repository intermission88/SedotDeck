//! DeckPort desktop app: a native window around deckport.py.
//!
//! The Python script keeps all the update logic. This shell starts it as a child process on a
//! free localhost port, then shows its UI in a webview window. With command-line arguments it
//! just runs the script's CLI, so `DeckPort.AppImage update` keeps working.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

const READY_PREFIX: &str = "DECKPORT_READY ";
const START_TIMEOUT: Duration = Duration::from_secs(15);

/// The running deckport.py server.
struct Backend(Mutex<Option<Child>>);

/// Where deckport.py was bundled. Tauri puts resources in different places per platform
/// (AppImage: usr/lib/<product>/, macOS: Contents/Resources/, dev builds: next to the binary).
fn script_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(p) = std::env::var("DECKPORT_SCRIPT") {
        out.push(PathBuf::from(p));
    }
    if let Some(dir) = std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.to_path_buf())) {
        out.push(dir.join("deckport.py"));
        out.push(dir.join("../lib/DeckPort/deckport.py"));
        out.push(dir.join("../lib/deckport/deckport.py"));
        out.push(dir.join("../Resources/deckport.py"));
        out.push(dir.join("../../../deckport.py")); // `cargo run` from desktop/src-tauri
    }
    out
}

fn find_script(app: Option<&tauri::AppHandle>) -> Option<PathBuf> {
    if let Some(app) = app {
        if let Ok(p) = app.path().resolve("deckport.py", tauri::path::BaseDirectory::Resource) {
            if p.is_file() {
                return Some(p);
            }
        }
    }
    script_candidates().into_iter().find(|p| p.is_file())
}

/// CLI mode: hand everything to `python3 deckport.py <args>`.
fn run_cli(args: Vec<String>) -> ! {
    let Some(script) = find_script(None) else {
        eprintln!("DeckPort: bundled deckport.py not found");
        std::process::exit(1);
    };
    let mut cmd = Command::new("python3");
    cmd.arg(script).args(args);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let err = cmd.exec(); // only returns on failure
        eprintln!("DeckPort needs Python 3 (python3 not found: {err})");
        std::process::exit(127);
    }
    #[cfg(not(unix))]
    {
        match cmd.status() {
            Ok(s) => std::process::exit(s.code().unwrap_or(1)),
            Err(err) => {
                eprintln!("DeckPort needs Python 3 (python3 not found: {err})");
                std::process::exit(127);
            }
        }
    }
}

fn fail(app: &tauri::AppHandle, message: &str) {
    app.dialog()
        .message(message)
        .title("DeckPort")
        .kind(MessageDialogKind::Error)
        .blocking_show();
    app.exit(1);
}

/// Start deckport.py, wait for its ready line, then point the window at it.
fn start_backend(app: tauri::AppHandle, origin: Arc<Mutex<Option<String>>>) {
    let Some(script) = find_script(Some(&app)) else {
        return fail(&app, "DeckPort is damaged: the bundled deckport.py is missing.");
    };
    let spawned = Command::new("python3")
        .arg(&script)
        .args(["serve", "--no-browser", "--port", "0", "--exit-on-stdin-close"])
        .stdin(Stdio::piped()) // closing this tells the server to quit
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn();
    let mut child = match spawned {
        Ok(c) => c,
        Err(_) => {
            return fail(
                &app,
                "DeckPort needs Python 3, but python3 was not found.\n\nSteamOS includes it by default; on other systems install python3 and try again.",
            )
        }
    };

    let (tx, rx) = mpsc::channel::<String>();
    let stdout = child.stdout.take().expect("stdout is piped");
    std::thread::spawn(move || {
        // Keep draining after the ready line so the server never blocks on a full pipe.
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(url) = line.strip_prefix(READY_PREFIX) {
                let _ = tx.send(url.trim().to_string());
            }
        }
    });
    *app.state::<Backend>().0.lock().unwrap() = Some(child);

    let url = match rx.recv_timeout(START_TIMEOUT) {
        Ok(u) => u,
        Err(_) => return fail(&app, "DeckPort's backend did not start within 15 seconds."),
    };
    let Ok(parsed) = Url::parse(&format!("{url}?app=1")) else {
        return fail(&app, "DeckPort's backend reported an invalid address.");
    };
    *origin.lock().unwrap() = Some(parsed.origin().ascii_serialization());
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.navigate(parsed);
    }
}

/// Window closed: close the server's stdin so it exits on its own. A download that is still
/// running finishes first (the server waits for it), so we don't kill it here.
fn stop_backend(app: &tauri::AppHandle) {
    let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() else {
        return;
    };
    drop(child.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if !args.is_empty() {
        run_cli(args);
    }

    let origin: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Backend(Mutex::new(None)))
        .setup({
            let origin = origin.clone();
            move |app| {
                let nav_origin = origin.clone();
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("DeckPort")
                    .inner_size(1100.0, 760.0)
                    .min_inner_size(720.0, 520.0)
                    .center()
                    // Only the bundled splash page and our own local server may load in the window.
                    .on_navigation(move |url| {
                        let local = matches!(url.scheme(), "tauri" | "about")
                            || url.host_str() == Some("tauri.localhost");
                        let backend = nav_origin.lock().unwrap().as_deref() == Some(url.origin().ascii_serialization().as_str());
                        local || backend
                    })
                    .build()?;
                let handle = app.handle().clone();
                std::thread::spawn(move || start_backend(handle, origin));
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building DeckPort")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_backend(app);
            }
        });
}
