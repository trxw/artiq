from types import SimpleNamespace

from migen import *
from migen.genlib.cdc import ElasticBuffer

from artiq.gateware.drtio import link_layer, rt_packets, iot, rt_controller, aux_controller


class GenericRXSynchronizer(Module):
    """Simple RX synchronizer based on the portable Migen elastic buffer.

    Introduces timing non-determinism in the satellite -> master path,
    (and in the echo_request/echo_reply RTT) but useful for testing.
    """
    def __init__(self):
        self.signals = []

    def resync(self, signal):
        synchronized = Signal.like(signal, related=signal)
        self.signals.append((signal, synchronized))
        return synchronized

    def do_finalize(self):
        eb = ElasticBuffer(sum(len(s[0]) for s in self.signals), 4, "rtio_rx", "rtio")
        self.submodules += eb
        self.comb += [
            eb.din.eq(Cat(*[s[0] for s in self.signals])),
            Cat(*[s[1] for s in self.signals]).eq(eb.dout)
        ]


class DRTIOSatellite(Module):
    def __init__(self, transceiver, channels, rx_synchronizer=None, fine_ts_width=3, full_ts_width=63):
        if rx_synchronizer is None:
            rx_synchronizer = GenericRXSynchronizer()
            self.submodules += rx_synchronizer

        self.submodules.link_layer = link_layer.LinkLayer(
            transceiver.encoder, transceiver.decoders)
        self.comb += self.link_layer.rx_ready.eq(transceiver.rx_ready)

        link_layer_sync = SimpleNamespace(
            tx_aux_frame=self.link_layer.tx_aux_frame,
            tx_aux_data=self.link_layer.tx_aux_data,
            tx_aux_ack=self.link_layer.tx_aux_ack,
            tx_rt_frame=self.link_layer.tx_rt_frame,
            tx_rt_data=self.link_layer.tx_rt_data,

            rx_aux_stb=rx_synchronizer.resync(self.link_layer.rx_aux_stb),
            rx_aux_frame=rx_synchronizer.resync(self.link_layer.rx_aux_frame),
            rx_aux_frame_perm=rx_synchronizer.resync(self.link_layer.rx_aux_frame_perm),
            rx_aux_data=rx_synchronizer.resync(self.link_layer.rx_aux_data),
            rx_rt_frame=rx_synchronizer.resync(self.link_layer.rx_rt_frame),
            rx_rt_frame_perm=rx_synchronizer.resync(self.link_layer.rx_rt_frame_perm),
            rx_rt_data=rx_synchronizer.resync(self.link_layer.rx_rt_data)
        )
        self.submodules.link_stats = link_layer.LinkLayerStats(link_layer_sync, "rtio")
        self.submodules.rt_packets = ClockDomainsRenamer("rtio")(
            rt_packets.RTPacketSatellite(link_layer_sync))

        self.submodules.iot = iot.IOT(
            self.rt_packets, channels, fine_ts_width, full_ts_width)

        self.clock_domains.cd_rio = ClockDomain()
        self.clock_domains.cd_rio_phy = ClockDomain()
        self.comb += [
            self.cd_rio.clk.eq(ClockSignal("rtio")),
            self.cd_rio.rst.eq(self.rt_packets.reset),
            self.cd_rio_phy.clk.eq(ClockSignal("rtio")),
            self.cd_rio_phy.rst.eq(self.rt_packets.reset_phy),
        ]

        self.submodules.aux_controller = aux_controller.AuxController(
            self.link_layer)

    def get_csrs(self):
        return (self.link_layer.get_csrs() + self.link_stats.get_csrs() +
                self.aux_controller.get_csrs())


class DRTIOMaster(Module):
    def __init__(self, transceiver, channel_count=1024, fine_ts_width=3):
        self.submodules.link_layer = link_layer.LinkLayer(
            transceiver.encoder, transceiver.decoders)
        self.comb += self.link_layer.rx_ready.eq(transceiver.rx_ready)

        self.submodules.link_stats = link_layer.LinkLayerStats(self.link_layer, "rtio_rx")
        self.submodules.rt_packets = rt_packets.RTPacketMaster(self.link_layer)
        self.submodules.rt_controller = rt_controller.RTController(
            self.rt_packets, channel_count, fine_ts_width)
        self.submodules.rt_manager = rt_controller.RTManager(self.rt_packets)
        self.cri = self.rt_controller.cri

        self.submodules.aux_controller = aux_controller.AuxController(
            self.link_layer)

    def get_csrs(self):
        return (self.link_layer.get_csrs() +
                self.link_stats.get_csrs() +
                self.rt_controller.get_csrs() +
                self.rt_manager.get_csrs() +
                self.aux_controller.get_csrs())
