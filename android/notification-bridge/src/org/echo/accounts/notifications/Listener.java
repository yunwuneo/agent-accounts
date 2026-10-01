package org.echo.accounts.notifications;

import android.app.Notification;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;

/** USB signals contain counters only; matching content stays in app-private history. */
public final class Listener extends NotificationListenerService {
    private final Object gate = new Object();
    private LocalServerSocket server;
    private volatile boolean alive;
    private boolean connected;
    private String target = "";
    private long sequence;
    private long posted, unmatched, summaries, groups, unreadable;
    private long activeDouyin, activeMatches;
    private final LinkedHashMap<String, String> seen = new LinkedHashMap<>();
    private HistoryStore history;

    static String hash(String value) {
        return NotificationRules.hash(value);
    }

    @Override public void onCreate() {
        super.onCreate();
        history = BridgeState.history(this);
        alive = true;
        new Thread(() -> {
            try {
                server = new LocalServerSocket("echo_douyin_notifications_v1");
                while (alive) {
                    try (LocalSocket socket = server.accept()) { serve(socket); }
                    catch (IOException ignored) { /* disconnected USB client; no payload logging */ }
                    finally { synchronized (gate) {
                        target = ""; seen.clear(); BridgeState.usb = false;
                    } }
                }
            } catch (IOException ignored) { /* client detects unavailable bridge */ }
        }, "echo-notification-bridge").start();
    }

    private void serve(LocalSocket socket) throws IOException {
        // adbd runs as shell (or root on an explicitly rooted device). Reject other apps.
        int uid = socket.getPeerCredentials().getUid();
        if (uid != 2000 && uid != 0) return;
        socket.setSoTimeout(5000);
        InputStream input = socket.getInputStream();
        StringBuilder line = new StringBuilder();
        for (int i = 0; i < 65; i++) {
            int c = input.read();
            if (c == '\n') break;
            if (c < 0) return;
            line.append((char)c);
        }
        if (!line.toString().matches("[a-f0-9]{64}")) return;
        synchronized (gate) {
            target = line.toString(); sequence = 0; seen.clear();
            history.setTarget(target);
            BridgeState.usb = true;
            posted = unmatched = summaries = groups = unreadable = 0;
            activeDouyin = activeMatches = 0;
            if (connected) {
                try {
                    captureActive();
                } catch (RuntimeException ignored) { unreadable++; }
            }
        }
        Writer out = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8);
        long sent = -1;
        while (alive) {
            synchronized (gate) {
                if (sent == sequence && connected) {
                    try { gate.wait(15000); }
                    catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
                }
                sent = sequence;
                out.write("{\"version\":1,\"connected\":" + connected + ",\"sequence\":" + sent
                    + ",\"posted\":" + posted + ",\"unmatched\":" + unmatched
                    + ",\"summaries\":" + summaries + ",\"groups\":" + groups
                    + ",\"unreadable\":" + unreadable + ",\"active_douyin\":" + activeDouyin
                    + ",\"active_matches\":" + activeMatches + "}\n");
                out.flush();
                if (!connected) return;
            }
        }
    }

    @Override public void onListenerConnected() {
        synchronized (gate) {
            connected = true; BridgeState.listening = true;
            try { captureActive(); } catch (RuntimeException ignored) { unreadable++; }
            gate.notifyAll();
        }
    }
    @Override public void onListenerDisconnected() {
        synchronized (gate) { connected = false; BridgeState.listening = false; gate.notifyAll(); }
    }
    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (!"com.ss.android.ugc.aweme".equals(sbn.getPackageName())) return;
        Notification n = sbn.getNotification();
        synchronized (gate) {
            if (history.target().isEmpty() || !connected) return;
            if (!target.isEmpty()) posted++;
            gate.notifyAll();
            try { process(sbn, n, false); }
            catch (RuntimeException ignored) { if (!target.isEmpty()) unreadable++; }
        }
    }

    private void captureActive() {
        activeDouyin = activeMatches = 0;
        StatusBarNotification[] items = getActiveNotifications();
        if (items == null) return;
        for (StatusBarNotification item : items) {
            if (!"com.ss.android.ugc.aweme".equals(item.getPackageName())) continue;
            activeDouyin++;
            try {
                if (classify(item.getNotification()) == 0) {
                    activeMatches++;
                    process(item, item.getNotification(), true);
                }
            } catch (RuntimeException ignored) { unreadable++; }
        }
    }

    private int classify(Notification n) {
        if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return 1;
        Notification.Style recovered = Notification.Builder.recoverBuilder(this, n).getStyle();
        Notification.MessagingStyle style = recovered instanceof Notification.MessagingStyle ? (Notification.MessagingStyle)recovered : null;
        String sender = null;
        if (style != null && !style.getMessages().isEmpty()) {
            Notification.MessagingStyle.Message last = style.getMessages().get(style.getMessages().size() - 1);
            if (last.getSenderPerson() != null) sender = String.valueOf(last.getSenderPerson().getName());
        }
        return NotificationRules.classify(false, style != null && style.isGroupConversation(),
            String.valueOf(n.extras.getCharSequence(Notification.EXTRA_TITLE, "")), sender, history.target());
    }

    private void process(StatusBarNotification sbn, Notification n, boolean active) {
            int classification = classify(n);
            if (classification != 0) {
                if (!active && !target.isEmpty()) {
                    if (classification == 1) summaries++;
                    if (classification == 2) groups++;
                    if (classification == 3) unmatched++;
                }
                return;
            }
            String title = String.valueOf(n.extras.getCharSequence(Notification.EXTRA_TITLE, ""));
            Notification.Style recovered = Notification.Builder.recoverBuilder(this, n).getStyle();
            Notification.MessagingStyle style = recovered instanceof Notification.MessagingStyle ? (Notification.MessagingStyle)recovered : null;
            // Hash transient content only, including MessagingStyle message updates under the same key.
            StringBuilder fingerprint = new StringBuilder(sbn.getKey()).append(sbn.getPostTime()).append(title)
                .append(n.extras.getCharSequence(Notification.EXTRA_TEXT, ""))
                .append(n.extras.getCharSequence(Notification.EXTRA_BIG_TEXT, ""));
            CharSequence[] lines = n.extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
            if (lines != null) for (CharSequence item : lines) fingerprint.append(item);
            if (style != null) for (Notification.MessagingStyle.Message item : style.getMessages())
                fingerprint.append(item.getTimestamp()).append(item.getText());
            String key = hash(sbn.getKey());
            String value = hash(fingerprint.toString());
            String text;
            if (style != null && !style.getMessages().isEmpty()) {
                text = String.valueOf(style.getMessages().get(style.getMessages().size() - 1).getText());
            } else {
                text = String.valueOf(n.extras.getCharSequence(Notification.EXTRA_BIG_TEXT, ""));
                if (text.isEmpty() && lines != null) {
                    StringBuilder body = new StringBuilder();
                    for (CharSequence item : lines) {
                        if (body.length() > 0) body.append('\n');
                        body.append(item);
                    }
                    text = body.toString();
                }
                if (text.isEmpty()) text = String.valueOf(n.extras.getCharSequence(Notification.EXTRA_TEXT, ""));
            }
            history.add(key, value, title, text, sbn.getPostTime(), active, System.currentTimeMillis());
            // History/active snapshots never replay past events into the monitor counter.
            if (active || target.isEmpty()) return;
            if (value.equals(seen.put(key, value))) return;
            if (seen.size() > 256) seen.remove(seen.keySet().iterator().next());
            sequence++;
            gate.notifyAll();
    }
    @Override public void onDestroy() {
        alive = false;
        synchronized (gate) {
            connected = false; BridgeState.listening = BridgeState.usb = false; gate.notifyAll();
        }
        try { if (server != null) server.close(); } catch (IOException ignored) {}
        super.onDestroy();
    }
}
