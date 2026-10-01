package org.echo.accounts.notifications;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

final class NotificationRules {
    static String hash(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte b : digest) result.append(String.format("%02x", b & 255));
            return result.toString();
        } catch (Exception e) { throw new IllegalStateException("hash unavailable"); }
    }

    static int classify(boolean summary, boolean group, String title, String sender, String target) {
        if (summary) return 1;
        if (group) return 2;
        if (target.isEmpty()) return 3;
        // MessagingStyle sender takes precedence, as in the original bridge.
        return hash(sender == null ? title : sender).equals(target) ? 0 : 3;
    }
}
