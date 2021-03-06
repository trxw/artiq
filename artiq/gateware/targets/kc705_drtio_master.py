#!/usr/bin/env python3.5

import argparse

from migen import *
from migen.build.generic_platform import *

from misoc.targets.kc705 import MiniSoC, soc_kc705_args, soc_kc705_argdict
from misoc.integration.soc_core import mem_decoder
from misoc.integration.builder import builder_args, builder_argdict

from artiq.gateware.ad9154_fmc_ebz import ad9154_fmc_ebz
from artiq.gateware.amp import AMPSoC, build_artiq_soc
from artiq.gateware import rtio
from artiq.gateware.rtio.phy import ttl_simple
from artiq.gateware.drtio.transceiver import gtx_7series
from artiq.gateware.drtio import DRTIOMaster
from artiq import __version__ as artiq_version


class Master(MiniSoC, AMPSoC):
    mem_map = {
        "rtio":          0x20000000,
        "rtio_dma":      0x30000000,
        "drtio_aux":     0x50000000,
        "mailbox":       0x70000000
    }
    mem_map.update(MiniSoC.mem_map)

    def __init__(self, cfg, medium, **kwargs):
        MiniSoC.__init__(self,
                         cpu_type="or1k",
                         sdram_controller_type="minicon",
                         l2_size=128*1024,
                         ident=artiq_version,
                         **kwargs)
        AMPSoC.__init__(self)

        platform = self.platform

        if medium == "sfp":
            self.comb += platform.request("sfp_tx_disable_n").eq(1)
            tx_pads = platform.request("sfp_tx")
            rx_pads = platform.request("sfp_rx")
        elif medium == "sma":
            tx_pads = platform.request("user_sma_mgt_tx")
            rx_pads = platform.request("user_sma_mgt_rx")
        else:
            raise ValueError

        if cfg == "simple_gbe":
            # GTX_1000BASE_BX10 Ethernet compatible, 62.5MHz RTIO clock
            # simple TTLs
            self.submodules.transceiver = gtx_7series.GTX_1000BASE_BX10(
                clock_pads=platform.request("sgmii_clock"),
                tx_pads=tx_pads,
                rx_pads=rx_pads,
                sys_clk_freq=self.clk_freq,
                clock_div2=True)
        elif cfg == "sawg_3g":
            # 3Gb link, 150MHz RTIO clock
            # with SAWG on local RTIO and AD9154-FMC-EBZ
            platform.register_extension(ad9154_fmc_ebz)
            self.submodules.transceiver = gtx_7series.GTX_3G(
                clock_pads=platform.request("ad9154_refclk"),
                tx_pads=tx_pads,
                rx_pads=rx_pads,
                sys_clk_freq=self.clk_freq)
        else:
            raise ValueError
        self.submodules.drtio = DRTIOMaster(self.transceiver)
        self.csr_devices.append("drtio")
        self.add_wb_slave(mem_decoder(self.mem_map["drtio_aux"]),
                          self.drtio.aux_controller.bus)
        self.add_memory_region("drtio_aux", self.mem_map["drtio_aux"] | self.shadow_base, 0x800)

        rtio_clk_period = 1e9/self.transceiver.rtio_clk_freq
        platform.add_period_constraint(self.transceiver.txoutclk, rtio_clk_period)
        platform.add_period_constraint(self.transceiver.rxoutclk, rtio_clk_period)
        platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.transceiver.txoutclk, self.transceiver.rxoutclk)

        rtio_channels = []
        for i in range(8):
            phy = ttl_simple.Output(platform.request("user_led", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy))
        for sma in "user_sma_gpio_p", "user_sma_gpio_n":
            phy = ttl_simple.Inout(platform.request(sma))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy))
        self.submodules.rtio_core = rtio.Core(rtio_channels, 3)
        self.csr_devices.append("rtio_core")

        self.submodules.rtio = rtio.KernelInitiator()
        self.submodules.rtio_dma = rtio.DMA(self.get_native_sdram_if())
        self.register_kernel_cpu_csrdevice("rtio")
        self.register_kernel_cpu_csrdevice("rtio_dma")
        self.submodules.cri_con = rtio.CRIInterconnectShared(
            [self.rtio.cri, self.rtio_dma.cri],
            [self.drtio.cri, self.rtio_core.cri])


def main():
    parser = argparse.ArgumentParser(
        description="ARTIQ device binary builder / KC705 DRTIO master")
    builder_args(parser)
    soc_kc705_args(parser)
    parser.add_argument("-c", "--config", default="simple_gbe",
                        help="configuration: simple_gbe/sawg_3g "
                             "(default: %(default)s)")
    parser.add_argument("--medium", default="sfp",
                        help="medium to use for transceiver link: sfp/sma "
                             "(default: %(default)s)")
    args = parser.parse_args()

    soc = Master(args.config, args.medium, **soc_kc705_argdict(args))
    build_artiq_soc(soc, builder_argdict(args))


if __name__ == "__main__":
    main()
