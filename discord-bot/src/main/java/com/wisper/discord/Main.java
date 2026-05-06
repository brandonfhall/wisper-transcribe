package com.wisper.discord;

import net.dv8tion.jda.api.JDA;
import net.dv8tion.jda.api.JDABuilder;
import net.dv8tion.jda.api.OnlineStatus;
import net.dv8tion.jda.api.audio.factory.AudioModuleConfig;
import net.dv8tion.jda.api.entities.Guild;
import net.dv8tion.jda.api.entities.channel.concrete.VoiceChannel;
import net.dv8tion.jda.api.events.session.ReadyEvent;
import net.dv8tion.jda.api.hooks.ListenerAdapter;
import net.dv8tion.jda.api.managers.AudioManager;
import net.dv8tion.jda.api.requests.GatewayIntent;
import net.dv8tion.jda.api.utils.MemberCachePolicy;
import club.minnced.discord.jdave.interop.JDaveSessionFactory;
import org.jetbrains.annotations.NotNull;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Path;

/**
 * Main entry point for the JDA Discord bot sidecar.
 *
 * Launched by Python BotManager as a subprocess.  Command-line args:
 *   --token <bot-token>
 *   --guild <guild-id>
 *   --voice-channel <channel-id>
 *   --socket <unix-socket-path>
 *
 * On launch:
 *   1. Connects to the Unix socket the Python side is listening on.
 *   2. Logs into Discord, joins the target voice channel.
 *   3. Routes per-user + combined decoded PCM to the socket.
 *   4. On disconnect, writes a __ctrl__ frame and exits.
 *
 * Shutdown triggers: stdin close (Python process exit), SIGTERM,
 * or unrecoverable disconnect.
 */
public final class Main extends ListenerAdapter {

    private static final Logger log = LoggerFactory.getLogger(Main.class);

    private final String token;
    private final String guildId;
    private final String voiceChannelId;
    private final Path socketPath;

    private SocketWriter socket;
    private JDA jda;
    private volatile boolean running = true;

    public Main(String token, String guildId, String voiceChannelId, Path socketPath) {
        this.token = token;
        this.guildId = guildId;
        this.voiceChannelId = voiceChannelId;
        this.socketPath = socketPath;
    }

    public void start() throws Exception {
        // 1. Connect to the Python-side Unix socket server
        socket = new SocketWriter(socketPath);
        log.info("Connected to Python socket at {}", socketPath);

        // 2. Build JDA with JDAVE wired in
        var audioConfig = new AudioModuleConfig()
                .withDaveSessionFactory(new JDaveSessionFactory());

        jda = JDABuilder.createDefault(token)
                .enableIntents(GatewayIntent.GUILD_VOICE_STATES)
                .setMemberCachePolicy(MemberCachePolicy.VOICE)
                .setStatus(OnlineStatus.INVISIBLE)
                .setAudioModuleConfig(audioConfig)
                .addEventListeners(this)
                .build();

        log.info("JDA connecting to Discord...");

        // 3. Monitor stdin for shutdown signal (Python side closes pipe on stop)
        Thread stdinWatcher = new Thread(this::watchStdin, "stdin-watcher");
        stdinWatcher.setDaemon(true);
        stdinWatcher.start();

        // 4. Block until shutdown
        while (running) {
            try {
                Thread.sleep(500);
            } catch (InterruptedException e) {
                break;
            }
        }

        shutdown(0);
    }

    private void watchStdin() {
        try {
            var in = System.in;
            byte[] buf = new byte[256];
            while (running) {
                int read = in.read(buf);
                if (read < 0) {
                    // stdin EOF — Python side closed pipe
                    log.info("stdin closed — shutting down");
                    running = false;
                    break;
                }
            }
        } catch (IOException e) {
            if (running) {
                log.info("stdin read error — shutting down: {}", e.getMessage());
                running = false;
            }
        }
    }

    // ── JDA events ──────────────────────────────────────────────────────────

    @Override
    public void onReady(@NotNull ReadyEvent event) {
        log.info("JDA ready. Joining voice channel {} in guild {}", voiceChannelId, guildId);

        Guild guild = jda.getGuildById(guildId);
        if (guild == null) {
            log.error("Guild not found: {}", guildId);
            running = false;
            return;
        }

        VoiceChannel vc = guild.getVoiceChannelById(voiceChannelId);
        if (vc == null) {
            log.error("Voice channel not found: {}", voiceChannelId);
            running = false;
            return;
        }

        AudioManager audioManager = guild.getAudioManager();

        // Register per-user audio dispatcher factory
        audioManager.setReceivingHandler(
                userId -> new UserAudioDispatcher(socket, userId)
        );

        audioManager.openAudioConnection(vc);
        log.info("Voice connection opened — recording started");
    }

    // ── Shutdown ────────────────────────────────────────────────────────────

    private void shutdown(int closeCode) {
        running = false;
        try {
            if (closeCode != 0 && socket != null) {
                socket.writeDisconnect(closeCode);
            }
        } catch (IOException ignored) {
        }
        if (socket != null) {
            socket.close();
        }
        if (jda != null) {
            jda.shutdown();
        }
        log.info("Sidecar shutdown complete (code={})", closeCode);
    }

    // ── Entry point ─────────────────────────────────────────────────────────

    public static void main(String[] args) {
        String token = null;
        String guildId = null;
        String voiceChannelId = null;
        Path socketPath = null;

        // Manual arg parsing (no extra deps)
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--token":
                    token = args[++i];
                    break;
                case "--guild":
                    guildId = args[++i];
                    break;
                case "--voice-channel":
                    voiceChannelId = args[++i];
                    break;
                case "--socket":
                    socketPath = Path.of(args[++i]);
                    break;
            }
        }

        if (token == null || guildId == null || voiceChannelId == null || socketPath == null) {
            System.err.println("Usage: discord-bot --token <token> --guild <guild> --voice-channel <vc> --socket <path>");
            System.exit(1);
        }

        // Install a shutdown hook for SIGTERM
        var main = new Main(token, guildId, voiceChannelId, socketPath);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> main.running = false));

        try {
            main.start();
        } catch (Exception e) {
            log.error("Fatal error: {}", e.getMessage(), e);
            System.exit(1);
        }

        System.exit(0);
    }
}
