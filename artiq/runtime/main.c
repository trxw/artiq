#include <stdio.h>
#include <string.h>
#include <alloc.h>
#include <irq.h>
#include <uart.h>
#include <console.h>
#include <system.h>
#include <generated/csr.h>
#include <hw/flags.h>

#include <lwip/init.h>
#include <lwip/ip4_addr.h>
#include <lwip/netif.h>
#include <lwip/timeouts.h>
#include <lwip/tcp.h>
#ifdef CSR_ETHMAC_BASE
#include <netif/etharp.h>
#include <liteethif.h>
#else
#include <netif/ppp/ppp.h>
#include <netif/ppp/pppos.h>
#endif

#include "flash_storage.h"

static struct netif netif;

#ifndef CSR_ETHMAC_BASE
static ppp_pcb *ppp;
#endif

void lwip_service(void);
void lwip_service(void)
{
    sys_check_timeouts();
#ifdef CSR_ETHMAC_BASE
    liteeth_input(&netif);
#else
    if(uart_read_nonblock()) {
        u8_t c;
        c = uart_read();
        pppos_input(ppp, &c, 1);
    }
#endif
}

#ifdef CSR_ETHMAC_BASE
unsigned char macadr[6];

static int hex2nib(int c)
{
    if((c >= '0') && (c <= '9'))
        return c - '0';
    if((c >= 'a') && (c <= 'f'))
        return c - 'a' + 10;
    if((c >= 'A') && (c <= 'F'))
        return c - 'A' + 10;
    return -1;
}

static void init_macadr(void)
{
    static const unsigned char default_macadr[6] = {0x10, 0xe2, 0xd5, 0x32, 0x50, 0x00};
#if (defined CSR_SPIFLASH_BASE && defined CONFIG_SPIFLASH_PAGE_SIZE)
    char b[32];
    char fs_macadr[6];
    int i, r, s;
#endif

    memcpy(macadr, default_macadr, 6);
#if (defined CSR_SPIFLASH_BASE && defined CONFIG_SPIFLASH_PAGE_SIZE)
    r = fs_read("mac", b, sizeof(b) - 1, NULL);
    if(r <= 0)
        return;
    b[r] = 0;
    for(i=0;i<6;i++) {
        r = hex2nib(b[3*i]);
        s = hex2nib(b[3*i + 1]);
        if((r < 0) || (s < 0))
            return;
        fs_macadr[i] = (r << 4) | s;
    }
    for(i=0;i<5;i++)
        if(b[3*i + 2] != ':')
            return;
    memcpy(macadr, fs_macadr, 6);
#endif
}

static void fsip_or_default(struct ip4_addr *d, char *key, int i1, int i2, int i3, int i4)
{
    int r;
#if (defined CSR_SPIFLASH_BASE && defined CONFIG_SPIFLASH_PAGE_SIZE)
    char cp[32];
#endif

    IP4_ADDR(d, i1, i2, i3, i4);
#if (defined CSR_SPIFLASH_BASE && defined CONFIG_SPIFLASH_PAGE_SIZE)
    r = fs_read(key, cp, sizeof(cp) - 1, NULL);
    if(r <= 0)
        return;
    cp[r] = 0;
    if(!ip4addr_aton(cp, d))
        return;
#endif
}

void network_init(void);
void network_init(void)
{
    struct ip4_addr local_ip;
    struct ip4_addr netmask;
    struct ip4_addr gateway_ip;

    init_macadr();
    fsip_or_default(&local_ip, "ip", 192, 168, 1, 50);
    fsip_or_default(&netmask, "netmask", 255, 255, 255, 0);
    fsip_or_default(&gateway_ip, "gateway", 192, 168, 1, 1);

    lwip_init();

    netif_add(&netif, &local_ip, &netmask, &gateway_ip, 0, liteeth_init, ethernet_input);
    netif_set_default(&netif);
    netif_set_up(&netif);
    netif_set_link_up(&netif);
}
#else /* CSR_ETHMAC_BASE */

static int ppp_connected;

static u32_t ppp_output_cb(ppp_pcb *pcb, u8_t *data, u32_t len, void *ctx)
{
    for(int i = 0; i < len; i++)
        uart_write(data[i]);
    return len;
}

static void ppp_status_cb(ppp_pcb *pcb, int err_code, void *ctx)
{
    if (err_code == PPPERR_NONE) {
        ppp_connected = 1;
        return;
    } else if (err_code == PPPERR_USER) {
        return;
    } else {
        ppp_connect(pcb, 1);
    }
}

void network_init(void)
{
    lwip_init();

    ppp_connected = 0;
    ppp = pppos_create(&netif, ppp_output_cb, ppp_status_cb, NULL);
    ppp_set_auth(ppp, PPPAUTHTYPE_NONE, "", "");
    ppp_set_default(ppp);
    ppp_connect(ppp, 0);

    while (!ppp_connected)
        lwip_service();
}

#endif /* CSR_ETHMAC_BASE */


extern void _fheap, _eheap;

extern void rust_main();

u16_t tcp_sndbuf_(struct tcp_pcb *pcb);
u16_t tcp_sndbuf_(struct tcp_pcb *pcb) {
    return tcp_sndbuf(pcb);
}

u8_t* tcp_so_options_(struct tcp_pcb *pcb);
u8_t* tcp_so_options_(struct tcp_pcb *pcb) {
    return &pcb->so_options;
}

void tcp_nagle_disable_(struct tcp_pcb *pcb);
void tcp_nagle_disable_(struct tcp_pcb *pcb) {
    tcp_nagle_disable(pcb);
}

int main(void)
{
    irq_setmask(0);
    irq_setie(1);
    uart_init();

    alloc_give(&_fheap, &_eheap - &_fheap);

    rust_main();

    return 0;
}
