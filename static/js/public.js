window.PageLnurlmintPublic = {
  template: '#page-lnurlmint-public',
  data() {
    return {
      mint: null,
      loading: true,
      notFound: false,
      copied: false
    }
  },
  computed: {
    mintId() {
      const parts = window.location.pathname.split('/')
      // /lnurlmint/m/{mint_id} → last segment
      return parts[parts.length - 1]
    },
    isOnion() {
      return window.location.hostname.endsWith('.onion')
    },
    showTorSection() {
      return this.mint && this.mint.onion_url && !this.isOnion
    }
  },
  async mounted() {
    await this.loadMintInfo()
  },
  methods: {
    async loadMintInfo() {
      this.loading = true
      try {
        const response = await fetch(
          `/lnurlmint/api/v1/public/${this.mintId}`
        )
        if (response.ok) {
          this.mint = await response.json()
        } else if (response.status === 404) {
          this.notFound = true
        } else {
          this.notFound = true
        }
      } catch (error) {
        console.error('Failed to load mint info:', error)
        this.notFound = true
      } finally {
        this.loading = false
      }
    },
    formatSats(msat) {
      if (!msat && msat !== 0) return '-'
      return (msat / 1000).toLocaleString() + ' sats'
    },
    async copyLnurl() {
      if (!this.mint || !this.mint.lnurl) return
      try {
        await navigator.clipboard.writeText(this.mint.lnurl)
        this.copied = true
        setTimeout(() => {
          this.copied = false
        }, 2000)
        this.$q.notify({ type: 'positive', message: 'LNURL copied!' })
      } catch (e) {
        this.$q.notify({ type: 'negative', message: 'Failed to copy.' })
      }
    },
    async copyOnion() {
      if (!this.mint || !this.mint.onion_url) return
      try {
        await navigator.clipboard.writeText(this.mint.onion_url)
        this.$q.notify({ type: 'positive', message: 'Tor address copied!' })
      } catch (e) {
        this.$q.notify({ type: 'negative', message: 'Failed to copy.' })
      }
    }
  }
}
