package wisper;

import club.minnced.discord.jdave.interop.JDaveSessionFactory;
import club.minnced.opus.util.OpusLibrary;
import net.dv8tion.jda.api.audio.AudioModuleConfig;
import net.dv8tion.jda.api.JDABuilder;
import net.dv8tion.jda.api.audio.AudioReceiveHandler;
import net.dv8tion.jda.api.audio.CombinedAudio;
import net.dv8tion.jda.api.audio.UserAudio;
import net.dv8tion.jda.api.entities.Guild;
import net.dv8tion.jda.api.entities.User;
import net.dv8tion.jda.api.entities.channel.concrete.VoiceChannel;
import net.dv8tion.jda.api.events.session.ReadyEvent;
import net.dv8tion.jda.api.hooks.ListenerAdapter;
import net.dv8tion.jda.api.managers.AudioManager;
import net.dv8tion.jda.api.requests.GatewayIntent;

import javax.sound.sampled.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Phase 0 JDA spike — verify DAVE per-user voice receive works.
 * Throwaway. Not retained in production.
 *
 * Usage:
 *   DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... DISCORD_CHANNEL_ID=...
 *   java --enable-native-access=ALL-UNNAMED -jar jda-spike.jar
 *
 * Records for RECORD_SECONDS (default 20), writes per-user PCM WAV files
 * to spike_output/, then exits.
 */
public class JdaSpike extends ListenerAdapter {

    private final long guildId;
    private final long channelId;
    private final int recordSeconds;
    private final Path outputDir;

    // per-user PCM accumulator: userId -> raw 48kHz stereo 16-bit PCM bytes
    private final Map<String, ByteArrayOutputStream> userBuffers = new ConcurrentHashMap<>();
    private final ByteArrayOutputStream mixedBuffer = new ByteArrayOutputStream();
    private final AtomicInteger frameCount = new AtomicInteger();

    public JdaSpike(long guildId, long channelId, int recordSeconds, Path outputDir) {
        this.guildId = guildId;
        this.channelId = channelId;
        this.recordSeconds = recordSeconds;
        this.outputDir = outputDir;
    }

    public static void main(String[] args) throws Exception {
        String token = System.getenv("DISCORD_BOT_TOKEN");
        long guildId = Long.parseLong(System.getenv("DISCORD_GUILD_ID"));
        long channelId = Long.parseLong(System.getenv("DISCORD_CHANNEL_ID"));
        int recordSeconds = Integer.parseInt(System.getenv().getOrDefault("RECORD_SECONDS", "20"));
        Path outputDir = Path.of("scripts/spike_output");

        if (token == null || token.isBlank()) {
            System.err.println("ERROR: DISCORD_BOT_TOKEN not set");
            System.exit(1);
        }

        // Load platform-native libopus. Try system install first (required on macOS ARM64
        // because opus-java-natives bundles x86_64-only darwin libs), then fall back to JAR.
        String sysOpus = System.getenv("OPUS_LIB");
        if (sysOpus != null && !sysOpus.isBlank()) {
            if (!OpusLibrary.loadFrom(sysOpus)) {
                System.err.println("WARNING: failed to load opus from OPUS_LIB=" + sysOpus + ", falling back to JAR");
                OpusLibrary.loadFromJar();
            } else {
                System.out.println("Loaded opus from: " + sysOpus);
            }
        } else {
            try { OpusLibrary.loadFromJar(); } catch (Exception e) {
                System.err.println("WARNING: bundled opus failed (" + e.getMessage() + "). Set OPUS_LIB=/path/to/libopus.dylib");
            }
        }

        JdaSpike spike = new JdaSpike(guildId, channelId, recordSeconds, outputDir);

        var audioConfig = new AudioModuleConfig()
                .withDaveSessionFactory(new JDaveSessionFactory());

        var jda = JDABuilder.createDefault(token)
                .enableIntents(GatewayIntent.GUILD_VOICE_STATES)
                .setAudioModuleConfig(audioConfig)
                .addEventListeners(spike)
                .build();

        jda.awaitReady();

        // Give the recording time to complete (on_ready handles join + record)
        Thread.sleep((recordSeconds + 10) * 1000L);
        jda.shutdown();
    }

    @Override
    public void onReady(ReadyEvent event) {
        System.out.println("Logged in as " + event.getJDA().getSelfUser().getName());
        System.out.println("JDA version: " + event.getJDA().getClass().getPackage().getImplementationVersion());

        Guild guild = event.getJDA().getGuildById(guildId);
        if (guild == null) {
            System.err.println("ERROR: guild " + guildId + " not found");
            System.exit(1);
        }

        VoiceChannel channel = guild.getVoiceChannelById(channelId);
        if (channel == null) {
            System.err.println("ERROR: voice channel " + channelId + " not found in " + guild.getName());
            System.exit(1);
        }

        System.out.println("Joining: " + channel.getName() + " in " + guild.getName());

        AudioManager audioManager = guild.getAudioManager();
        audioManager.setReceivingHandler(new AudioReceiveHandler() {
            @Override
            public boolean canReceiveCombined() { return true; }

            @Override
            public boolean canReceiveUser() { return true; }

            @Override
            public void handleUserAudio(UserAudio userAudio) {
                User user = userAudio.getUser();
                String userId = user.getId() + "_" + user.getName().replaceAll("[^a-zA-Z0-9]", "_");
                byte[] pcm = userAudio.getAudioData(1.0);
                userBuffers.computeIfAbsent(userId, k -> new ByteArrayOutputStream()).writeBytes(pcm);
                frameCount.incrementAndGet();
            }

            @Override
            public void handleCombinedAudio(CombinedAudio combinedAudio) {
                mixedBuffer.writeBytes(combinedAudio.getAudioData(1.0));
            }
        });

        audioManager.openAudioConnection(channel);
        System.out.println("Connected. Recording for " + recordSeconds + "s — speak into the channel...");
        System.out.flush();

        // Stop after recordSeconds
        Thread.ofVirtual().start(() -> {
            try {
                Thread.sleep(recordSeconds * 1000L);
            } catch (InterruptedException ignored) {}

            System.out.println("\nStopping. Frames received: " + frameCount.get());
            audioManager.closeAudioConnection();

            try {
                Files.createDirectories(outputDir);
                writeSummary();
            } catch (Exception e) {
                System.err.println("ERROR writing output: " + e.getMessage());
                System.exit(1);
            }
            System.exit(0);
        });
    }

    private void writeSummary() throws Exception {
        if (frameCount.get() == 0) {
            System.out.println("WARNING: no audio frames received — DAVE decryption may have failed.");
            System.out.println("  Check jdave-api version and --enable-native-access flag.");
            return;
        }

        // Write per-user WAV files (48kHz stereo 16-bit PCM — JDA native format)
        for (var entry : userBuffers.entrySet()) {
            Path wavFile = outputDir.resolve(entry.getKey() + ".wav");
            writePcmAsWav(entry.getValue().toByteArray(), wavFile);
            System.out.println("  Wrote " + wavFile + " (" + Files.size(wavFile) + " bytes)");
        }

        // Write mixed WAV
        if (mixedBuffer.size() > 0) {
            Path mixedFile = outputDir.resolve("__mixed__.wav");
            writePcmAsWav(mixedBuffer.toByteArray(), mixedFile);
            System.out.println("  Wrote " + mixedFile + " (" + Files.size(mixedFile) + " bytes)");
        }

        System.out.println("\nDone. " + userBuffers.size() + " per-user track(s) + mixed.");
        System.out.println("Play with: ffplay -f s16le -ar 48000 -ac 2 " + outputDir + "/<user>.wav");
        System.out.println("Or open the .wav files directly in any audio player.");
    }

    private static void writePcmAsWav(byte[] pcm, Path out) throws Exception {
        // 48kHz stereo 16-bit signed little-endian — JDA's native output format
        AudioFormat format = new AudioFormat(48000f, 16, 2, true, false);
        AudioInputStream ais = new AudioInputStream(
                new ByteArrayInputStream(pcm),
                format,
                pcm.length / format.getFrameSize()
        );
        AudioSystem.write(ais, AudioFileFormat.Type.WAVE, out.toFile());
    }
}
