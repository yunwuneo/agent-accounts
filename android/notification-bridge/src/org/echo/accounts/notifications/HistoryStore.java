package org.echo.accounts.notifications;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.*;

/** Bounded, app-private history. This data is never part of the USB protocol. */
final class HistoryStore {
    static final int LIMIT = 100;
    static final long RETENTION_MS = 7L * 24 * 60 * 60 * 1000;
    static final class Entry {
        final String key, fingerprint, title, text;
        final long time;
        final boolean active;
        Entry(String key, String fingerprint, String title, String text, long time, boolean active) {
            this.key = key; this.fingerprint = fingerprint; this.title = title;
            this.text = text; this.time = time; this.active = active;
        }
    }
    private final File file;
    private String target = "";
    private final List<Entry> entries = new ArrayList<>();
    // Kept after clearing visible history so reconnecting cannot resurrect it.
    private final LinkedHashMap<String, String> seen = new LinkedHashMap<>();
    private long revision;
    private boolean storageError;

    HistoryStore(File file, long now) {
        this.file = file;
        if (file.exists()) {
            try (DataInputStream in = new DataInputStream(new FileInputStream(file))) {
                if (file.length() > 2_000_000 || in.readInt() != 1) throw new IOException();
                target = in.readUTF();
                if (!target.isEmpty() && !target.matches("[a-f0-9]{64}")) throw new IOException();
                int count = in.readInt();
                if (count < 0 || count > LIMIT) throw new IOException();
                for (int i = 0; i < count; i++) {
                    String key = in.readUTF(), fingerprint = in.readUTF();
                    String title = in.readUTF(), text = in.readUTF();
                    long time = in.readLong();
                    boolean active = in.readBoolean();
                    if (title.length() > 256 || text.length() > 4000 || time < 0
                        || !key.matches("[a-f0-9]{64}") || !fingerprint.matches("[a-f0-9]{64}"))
                        throw new IOException();
                    entries.add(new Entry(key, fingerprint, title, text, time, active));
                }
                count = in.readInt();
                if (count < 0 || count > 256) throw new IOException();
                for (int i = 0; i < count; i++) {
                    String key = in.readUTF(), value = in.readUTF();
                    if (!key.matches("[a-f0-9]{64}") || !value.matches("[a-f0-9]{64}"))
                        throw new IOException();
                    seen.put(key, value);
                }
                if (in.read() != -1 || (target.isEmpty() && !entries.isEmpty())) throw new IOException();
            } catch (IOException ignored) {
                // Fail closed on corrupt state; never expose partial or differently scoped history.
                target = ""; entries.clear(); seen.clear(); storageError = true;
            }
        }
        if (prune(now)) save();
    }

    synchronized String target() { return target; }
    synchronized long revision() { return revision; }
    synchronized boolean storageError() { return storageError; }

    synchronized void setTarget(String value) {
        if (!value.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("invalid target hash");
        if (value.equals(target)) return;
        target = value; entries.clear(); seen.clear(); revision++; save();
    }

    synchronized boolean add(String key, String fingerprint, String title, String text,
                             long time, boolean active, long now) {
        if (target.isEmpty()) return false;
        boolean pruned = prune(now);
        if (time < now - RETENTION_MS || time > now || fingerprint.equals(seen.get(key))) {
            if (pruned) save();
            return false;
        }
        seen.remove(key); seen.put(key, fingerprint);
        if (seen.size() > 256) seen.remove(seen.keySet().iterator().next());
        entries.add(new Entry(key, fingerprint, clip(title, 256), clip(text, 4000), time, active));
        entries.sort((a, b) -> Long.compare(b.time, a.time));
        while (entries.size() > LIMIT) entries.remove(entries.size() - 1);
        revision++; save();
        return true;
    }

    synchronized List<Entry> snapshot(long now) {
        if (prune(now)) save();
        return new ArrayList<>(entries);
    }

    synchronized void clear() {
        entries.clear(); revision++; save();
    }

    private boolean prune(long now) {
        boolean changed = entries.removeIf(e -> e.time < now - RETENTION_MS || e.time > now);
        if (changed) revision++;
        return changed;
    }

    static String clip(String value, int limit) {
        if (value == null) return "";
        if (value.length() <= limit) return value;
        int end = limit - 1;
        if (Character.isHighSurrogate(value.charAt(end - 1))) end--;
        return value.substring(0, end) + "…";
    }

    private void save() {
        File temp = new File(file.getPath() + ".tmp");
        try {
            try (FileOutputStream raw = new FileOutputStream(temp);
                 DataOutputStream out = new DataOutputStream(raw)) {
                out.writeInt(1); out.writeUTF(target); out.writeInt(entries.size());
                for (Entry e : entries) {
                    out.writeUTF(e.key); out.writeUTF(e.fingerprint); out.writeUTF(e.title);
                    out.writeUTF(e.text); out.writeLong(e.time); out.writeBoolean(e.active);
                }
                out.writeInt(seen.size());
                for (Map.Entry<String, String> e : seen.entrySet()) {
                    out.writeUTF(e.getKey()); out.writeUTF(e.getValue());
                }
                out.flush(); raw.getFD().sync();
            }
            Files.move(temp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING,
                       StandardCopyOption.ATOMIC_MOVE);
            storageError = false;
        } catch (IOException ignored) {
            storageError = true; // UI reports persistence failure; never log content.
        } finally { temp.delete(); }
    }
}
